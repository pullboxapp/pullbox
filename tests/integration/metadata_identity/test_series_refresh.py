"""Actual refresh must preserve the library while provider requests are in flight."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from pullbox.core.metadata_identity import IdentityNamespace as Namespace
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.models import Issue, Series
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_baseline import SeriesMetadataBaseline
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import IssueCatalogState, SeriesStatusOverride
from pullbox.schemas.metadata_sources import MetadataFetch, MetadataPage, SourceStatus
from pullbox.services.metadata_baselines import load_metadata_baseline
from pullbox.services.metadata_series_adoption import adopt_source_series_bundle
from pullbox.services.metadata_series_refresh import SeriesRefreshError, refresh_series_from_sources
from tests.integration.metadata_identity.test_series_adoption import (  # noqa: F401
    bundle,
    configured_sources,
)
from tests.unit.test_metadata_source_reads import ReadAdapter, registry


async def test_linked_metron_enriches_gcd_series_without_taking_over_issues(
    identity_probe_db, tmp_path
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from pullbox.core.metadata_identity import (
        ExactIdentityEvidence,
        ExternalIdentityRef,
        IdentityEvidenceKind,
    )
    from pullbox.core.metadata_identity_events import IdentityEventEvidence, IdentityEventRequest
    from pullbox.core.metadata_identity_state import IdentityVerificationAction as Action
    from pullbox.models.library import LibraryFile, LibraryRoot
    from pullbox.models.reader import IssueReaderState
    from pullbox.models.user import User
    from pullbox.schemas.metadata_sources import SourceCapability
    from pullbox.services.metadata_discovery import MetadataSourceRegistry
    from pullbox.services.metadata_identity_attachment import attach_verified_identities
    from tests.unit.test_metadata_discovery import registration, runtime

    _, factory, _ = identity_probe_db
    native = bundle(numbers=("1", "2"))
    generation = datetime(2026, 9, 29, tzinfo=UTC)
    gcd = replace(
        native,
        series=native.series.model_copy(
            update={
                "source": Source.GCD_LOCAL,
                "identity_namespace": Namespace.GCD,
                "description": None,
                "image_url": None,
                "source_updated_at": generation,
            }
        ),
        issues=tuple(
            item.model_copy(
                update={
                    "source": Source.GCD_LOCAL,
                    "identity_namespace": Namespace.GCD,
                    "source_updated_at": generation,
                }
            )
            for item in native.issues
        ),
    )
    cover = "https://static.metron.cloud/media/issue/enriched.jpg"
    artifact = tmp_path / "kept.cbz"
    artifact.write_bytes(b"unchanged comic archive")
    native.series.description = "Metron description"
    native.issues[0].image_url = cover
    async with factory.begin() as session:
        adopted = await adopt_source_series_bundle(session, gcd)
        series_id = adopted.series.id
        adopted.series.path = "/reference/untouched"
        adopted.series.title = "My title"
        first = await session.scalar(select(Issue).order_by(Issue.id))
        first.status = IssueStatus.OWNED
        issue_id = first.id
        root = LibraryRoot(name="Reference", path=str(tmp_path))
        user = User(username="reader", password_hash="not-a-live-account")
        session.add_all([root, user])
        await session.flush()
        session.add(
            LibraryFile(
                issue_id=issue_id,
                library_root_id=root.id,
                file_path=str(artifact),
                file_name=artifact.name,
                file_format="cbz",
                file_size=artifact.stat().st_size,
                file_modified_at=generation,
                storage_mode="referenced",
            )
        )
        session.add(
            IssueReaderState(user_id=user.id, issue_id=issue_id, last_page_index=17, page_count=24)
        )
        identity = ExternalIdentityRef(Namespace.METRON, Kind.SERIES, "42")
        await attach_verified_identities(
            session,
            [
                IdentityEventRequest(
                    uuid4(),
                    series_id,
                    Action.VERIFY,
                    IdentityEventEvidence(
                        ExactIdentityEvidence(
                            identity, IdentityEvidenceKind.PROVIDER_RESULT, Source.METRON_API
                        ),
                        "a" * 64,
                        source_identity=identity,
                    ),
                )
            ],
        )
    async with factory() as session:
        adapters = {
            source: RefreshAdapter(data, session=session)
            for source, data in ((Source.GCD_LOCAL, gcd), (Source.METRON_API, native))
        }
        sources = MetadataSourceRegistry(
            [runtime(source, revision=1) for source in adapters],
            factories={
                source: registration(adapter, capabilities=list(SourceCapability))
                for source, adapter in adapters.items()
            },
        )
        result = await refresh_series_from_sources(session, series_id, registry=sources)
        assert result.description == "Metron description"
        assert result.cover_url == cover
        assert result.title == "My title" and result.path == "/reference/untouched"
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == 2
        assert (await session.get(Issue, issue_id)).status is IssueStatus.OWNED
        assert set(await session.scalars(select(IssueExternalIdentity.identity_namespace))) == {
            Namespace.GCD
        }
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        file = await session.scalar(select(LibraryFile))
        assert file.issue_id == issue_id and file.file_path == str(artifact)
        assert file.storage_mode.value == "referenced"
        assert (await session.scalar(select(IssueReaderState))).last_page_index == 17
    assert artifact.read_bytes() == b"unchanged comic archive"


class RefreshAdapter(ReadAdapter):
    def __init__(self, data, *, wait=None, session=None):
        super().__init__(wait=wait)
        self.data = data
        self.session = session
        self.failure = None
        self.profile_failure = None

    async def series(self, external_id, *, validator=None):
        assert self.session is None or not self.session.in_transaction()
        self.calls.append(("series", external_id))
        self.started.set()
        if self.wait:
            await self.wait.wait()
        if self.profile_failure:
            return MetadataFetch(status=self.profile_failure)
        return MetadataFetch(status=SourceStatus.OK, data=self.data.series)

    async def issues(self, external_id, *, page=1, validator=None):
        assert self.session is None or not self.session.in_transaction()
        self.calls.append(("issues", external_id, page))
        if self.failure:
            return MetadataFetch(status=self.failure)
        start = (page - 1) * 100
        end = min(start + 100, len(self.data.issues))
        return MetadataFetch(
            status=SourceStatus.OK,
            data=MetadataPage(
                results=list(self.data.issues[start:end]),
                total=len(self.data.issues),
                next_page=page + 1 if end < len(self.data.issues) else None,
            ),
        )


def refresh_registry(adapter):
    result = registry(adapter, total_timeout=60)
    result.runtime[Source.METRON_API].policy.revision = 1
    return result


async def seed(factory, *, count=1):
    data = bundle(numbers=tuple(str(i + 1) for i in range(count)))
    data.series.description = "Original provider description"
    data.issues[0].title = "Original issue title"
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data, monitored=True)
        result.series.path = "/reference/original"
        series_id = result.series.id
        issue = await session.scalar(select(Issue).order_by(Issue.id))
        issue.status = IssueStatus.OWNED
        issue.manual_skip = True
        issue_id = issue.id
    return series_id, issue_id, data


async def test_native_refresh_updates_managed_values_and_adds_complete_lettered_catalog(
    identity_probe_db,
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, _ = await seed(factory)
    data = bundle(numbers=("1", "50-x", "50-o"))
    data.series.description = "Updated provider description"
    data.issues[0].title = "Updated issue title"
    async with factory() as session:
        adapter = RefreshAdapter(data, session=session)
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(adapter)
        )
        assert result is not None, "The ordinary refresh must support a native Metron series"
        assert result.description == "Updated provider description"
        assert result.comicvine_id is None and result.path == "/reference/original"
        assert result.issue_count == 3 and result.issue_catalog_state is IssueCatalogState.COMPLETE
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [item.issue_number_text for item in issues] == ["1", "50-X", "50-O"]
        assert issues[0].id == issue_id and issues[0].status is IssueStatus.OWNED
        assert issues[0].manual_skip and issues[0].title == "Updated issue title"
        assert all(item.status is IssueStatus.WANTED for item in issues[1:])
        saved = await load_metadata_baseline(session, Kind.SERIES, series_id)
        assert saved.revision == 2 and saved.snapshot.values.description == result.description
        assert (await load_metadata_baseline(session, Kind.ISSUE, issue_id)).revision == 2
    assert adapter.calls == [("series", "42"), ("issues", "42", 1)]


async def test_refresh_preserves_local_edits_and_explicit_status_override(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data = await seed(factory)
    async with factory.begin() as session:
        series = await session.get(Series, series_id)
        series.description = "My description"
        series.status_override = SeriesStatusOverride.ENDED
        series.status = "ended"
        issue = await session.get(Issue, issue_id)
        issue.title = None
    data.series.description = "Provider replacement"
    data.series.status = "continuing"
    data.issues[0].title = "Provider issue replacement"
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data))
        )
        assert result is not None
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).description == "My description"
        assert (await session.get(Series, series_id)).status.value == "ended"
        assert (await session.get(Issue, issue_id)).title is None
        saved = await load_metadata_baseline(session, Kind.ISSUE, issue_id)
        assert next(
            origin for origin in saved.snapshot.origins if origin.field == "title"
        ).user_override


@pytest.mark.parametrize(
    "change", ["series_edit", "issue_edit", "identity", "policy", "baseline", "credits"]
)
async def test_inflight_change_rejects_whole_refresh_without_losing_newer_state(
    identity_probe_db, change
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data = await seed(factory)
    data.series.description = "Late provider data"
    adapter = RefreshAdapter(data, wait=asyncio.Event())
    async with factory() as session:
        task = asyncio.create_task(
            refresh_series_from_sources(session, series_id, registry=refresh_registry(adapter))
        )
        started = asyncio.create_task(adapter.started.wait())
        done, _ = await asyncio.wait(
            [task, started], timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if started not in done:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            assert not task.done(), "Refresh did not reach its bounded provider read"
        try:
            async with factory.begin() as editor:
                if change == "series_edit":
                    await editor.execute(
                        update(Series)
                        .where(Series.id == series_id)
                        .values(description="Newer user edit")
                    )
                elif change == "issue_edit":
                    await editor.execute(
                        update(Issue).where(Issue.id == issue_id).values(title="Newer issue edit")
                    )
                elif change == "credits":
                    author = Creator(name="User added author")
                    editor.add(author)
                    await editor.flush()
                    editor.add(IssueCreator(issue_id=issue_id, creator_id=author.id, role="writer"))
                elif change == "identity":
                    await editor.execute(
                        update(SeriesExternalIdentity).values(
                            revision=2, verification_state="stale"
                        )
                    )
                elif change == "policy":
                    await editor.execute(
                        update(MetadataSourceConfig)
                        .where(MetadataSourceConfig.source == Source.METRON_API.value)
                        .values(revision=2)
                    )
                else:
                    await editor.execute(update(SeriesMetadataBaseline).values(revision=2))
            adapter.wait.set()
            with pytest.raises(SeriesRefreshError, match="changed"):
                await task
            await session.commit()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        current = await session.get(Series, series_id)
        assert current.description == (
            "Newer user edit" if change == "series_edit" else "Original provider description"
        )
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1


@pytest.mark.parametrize(
    "change", ["missing", "renumbered", "wrong_parent", "replaced_identity", "source_failure"]
)
async def test_catalog_disagreement_never_deletes_or_reassigns_existing_issue(
    identity_probe_db, change
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data = await seed(factory)
    if change == "missing":
        data.series.issue_count = 0
        data = replace(data, issues=(), catalog_total=0)
    elif change == "renumbered":
        data.issues[0].issue_number_text = "2"
    elif change == "wrong_parent":
        data.issues[0].series_external_id = "99"
    elif change == "replaced_identity":
        data.issues[0].external_id = "999"
    adapter = RefreshAdapter(data)
    if change == "source_failure":
        adapter.failure = SourceStatus.RATE_LIMITED
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(adapter)
            )
        await session.commit()
    async with factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue.issue_number_text == "1" and issue.status is IssueStatus.OWNED
        assert (await session.scalar(select(IssueExternalIdentity))).external_id == "100"
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 1


async def test_refresh_does_not_invent_provenance_for_legacy_user_metadata(identity_probe_db):
    from sqlalchemy import delete

    from pullbox.models.metadata_baseline import IssueMetadataBaseline

    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    async with factory.begin() as session:
        await session.execute(delete(SeriesMetadataBaseline))
        await session.execute(delete(IssueMetadataBaseline))
    data.series.description = "Different provider text"
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data))
        )
        assert result is not None
        assert result.description == "Original provider description"
        await session.commit()
    async with factory() as session:
        saved = await load_metadata_baseline(session, Kind.SERIES, series_id)
        origin = next(item for item in saved.snapshot.origins if item.field == "description")
        assert origin.source is None


async def test_existing_refresh_endpoint_uses_native_identity_and_preserves_response(
    identity_probe_db, monkeypatch
):
    from pullbox.api.v1 import series as routes
    from pullbox.models.user import User
    from pullbox.services import metadata_series_refresh as refresh

    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    data.series.description = "Endpoint refreshed"
    adapter = RefreshAdapter(data)
    instance = refresh_registry(adapter)
    monkeypatch.setattr(refresh, "MetadataSourceRegistry", lambda *args, **kwargs: instance)
    async with factory() as session:
        response = await routes.refresh_series(series_id, User(username="tester"), session)
        assert response.description == "Endpoint refreshed"
        assert response.id == series_id
        await session.commit()
    assert adapter.calls == [("series", "42"), ("issues", "42", 1)]


async def test_hundred_issue_refresh_keeps_database_reads_bounded(identity_probe_db):
    from sqlalchemy import event

    engine, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory, count=101)
    reads = []

    def record(_conn, _cursor, statement, *_rest):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as session:
            result = await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(RefreshAdapter(data))
            )
            assert result.issue_count == 101
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(reads) < 60, f"Refresh issued {len(reads)} reads for 101 issues"


async def test_new_issue_crosswalk_cannot_duplicate_an_owned_issue(identity_probe_db):
    from pullbox.core.metadata_identity import ExternalIdentityRef

    _, factory, _ = identity_probe_db
    series_id, _, _ = await seed(factory)
    async with factory.begin() as session:
        other = Series(title="Other", sort_title="Other")
        session.add(other)
        await session.flush()
        session.add(
            Issue(series_id=other.id, comicvine_id=900, issue_number=2, status=IssueStatus.OWNED)
        )
    data = bundle(numbers=("1", "2"))
    data.issues[1].cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "900")]
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(RefreshAdapter(data))
            )
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == 2


async def test_cancellation_drains_provider_and_leaves_catalog_and_baseline_unchanged(
    identity_probe_db,
):
    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    adapter = RefreshAdapter(data, wait=asyncio.Event())
    async with factory() as session:
        task = asyncio.create_task(
            refresh_series_from_sources(session, series_id, registry=refresh_registry(adapter))
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not session.in_transaction()
    assert adapter.closed == 1
    async with factory() as session:
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 1


async def test_caller_rollback_undoes_catalog_addition_and_baselines(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, _ = await seed(factory)
    data = bundle(numbers=("1", "2"))
    async with factory() as session:
        await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data))
        )
        await session.rollback()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 1


async def test_dirty_session_is_not_rolled_back_by_refresh(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    adapter = RefreshAdapter(data)
    async with factory() as session:
        current = await session.get(Series, series_id)
        current.title = "Pending user edit"
        with pytest.raises(SeriesRefreshError, match="pending"):
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(adapter)
            )
        assert current.title == "Pending user edit"
        await session.commit()
    assert adapter.calls == []


async def test_background_fill_does_not_replace_managed_descriptions(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data = await seed(factory)
    data.series.description = "Replacement"
    data.issues[0].title = "Replacement issue"
    data.issues[0].description = "Previously missing"
    async with factory() as session:
        result = await refresh_series_from_sources(
            session,
            series_id,
            registry=refresh_registry(RefreshAdapter(data)),
            replace_managed=False,
        )
        assert result.description == "Original provider description"
        issue = await session.get(Issue, issue_id)
        assert issue.title == "Original issue title" and issue.description == "Previously missing"
        await session.commit()


async def test_failed_response_rolls_back_actual_refresh_command(identity_probe_db, monkeypatch):
    from pullbox.api.v1 import series as routes
    from pullbox.models.user import User
    from pullbox.services import metadata_series_refresh as refresh

    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    data.series.description = "Must roll back"
    instance = refresh_registry(RefreshAdapter(data))
    monkeypatch.setattr(refresh, "MetadataSourceRegistry", lambda *args, **kwargs: instance)

    async def fail(*_args):
        raise RuntimeError("response failed")

    monkeypatch.setattr(routes, "_load_series_response", fail)
    async with factory() as session:
        with pytest.raises(RuntimeError, match="response failed"):
            await routes.refresh_series(series_id, User(username="tester"), session)
    async with factory() as session:
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 1
        assert (await session.get(Series, series_id)).description == "Original provider description"


@pytest.mark.parametrize("race", [False, True])
async def test_cover_fetch_occurs_after_commit_and_rechecks_url(
    identity_probe_db, tmp_path, monkeypatch, race
):
    from pullbox.api.v1 import series as routes
    from pullbox.models.user import User
    from pullbox.services import metadata_series_refresh as refresh

    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    url = "https://static.metron.cloud/media/new.jpg"
    data.series.image_url = url
    data.series.description = "Committed before artwork"
    instance = refresh_registry(RefreshAdapter(data))
    monkeypatch.setattr(refresh, "MetadataSourceRegistry", lambda *args, **kwargs: instance)

    async def covers(_session):
        return tmp_path

    monkeypatch.setattr(refresh, "resolve_covers_dir", covers)
    async with factory() as session:

        async def download(_client, actual_url, destination):
            assert actual_url == url and not session.in_transaction()
            async with factory.begin() as observer:
                current = await observer.get(Series, series_id)
                assert current.description == "Committed before artwork"
                if race:
                    current.cover_url = "https://static.metron.cloud/media/newer.jpg"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"test artwork")
            return True

        monkeypatch.setattr(refresh.ProviderArtworkClient, "download_cover", download)
        await routes.refresh_series(series_id, User(username="tester"), session)
    assert (tmp_path / str(series_id) / "series.jpg").exists() is (not race)
    assert list(tmp_path.rglob(".*series.jpg")) == []


@pytest.mark.parametrize("failure", ["download", "publish", "database"])
async def test_artwork_failure_does_not_report_committed_metadata_as_failed(
    identity_probe_db, tmp_path, monkeypatch, failure
):
    from pullbox.api.v1 import series as routes
    from pullbox.models.user import User
    from pullbox.services import metadata_series_refresh as refresh

    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    data.series.image_url = "https://static.metron.cloud/media/new.jpg"
    data.series.description = "Committed metadata"
    instance = refresh_registry(RefreshAdapter(data))
    monkeypatch.setattr(refresh, "MetadataSourceRegistry", lambda *args, **kwargs: instance)

    async def covers(_session):
        return tmp_path

    destination = tmp_path / str(series_id) / "series.jpg"
    destination.parent.mkdir()
    destination.write_bytes(b"existing cover")

    async def download(_client, _url, pending):
        if failure == "download":
            raise PermissionError("private storage diagnostic")
        pending.write_bytes(b"replacement cover")
        if failure == "database":

            async def fail_cache_read(*args, **kwargs):
                raise OperationalError("cover lookup", {}, Exception("private storage diagnostic"))

            monkeypatch.setattr(session, "scalar", fail_cache_read)
        return True

    original_replace = Path.replace

    def reject_publish(path, target):
        if target == destination:
            raise PermissionError("private storage diagnostic")
        return original_replace(path, target)

    monkeypatch.setattr(refresh, "resolve_covers_dir", covers)
    monkeypatch.setattr(refresh.ProviderArtworkClient, "download_cover", download)
    monkeypatch.setattr(Path, "replace", reject_publish)
    with capture_logs() as logs:
        # A logger cached before app reconfiguration still holds the old processor list.
        monkeypatch.setattr(
            refresh, "logger", structlog.wrap_logger(None, cache_logger_on_first_use=False)
        )
        async with factory() as session:
            response = await routes.refresh_series(series_id, User(username="tester"), session)
            assert response.description == "Committed metadata"
            assert not session.in_transaction()
    async with factory() as session:
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 2
    assert destination.read_bytes() == b"existing cover"
    assert list(tmp_path.rglob(".*series.jpg")) == []
    assert any(item["event"] == "metadata_series_artwork_refresh_failed" for item in logs)
    assert "private storage diagnostic" not in str(logs)


async def test_core_and_catalog_use_independent_configured_priorities(identity_probe_db):
    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.schemas.metadata_sources import MetadataDomain, SourceCapability
    from pullbox.services.metadata_discovery import MetadataSourceRegistry
    from pullbox.services.metadata_identity_attachment import attach_verified_identities
    from pullbox.services.metadata_series_adoption import _request
    from pullbox.services.metadata_sources import SourceRuntime, read_source_policies
    from tests.unit.test_metadata_discovery import registration

    _, factory, _ = identity_probe_db
    series_id, issue_id, metron_data = await seed(factory)
    cv_data = bundle(numbers=("1", "2"))
    cv_data.series.source = Source.COMICVINE_API
    cv_data.series.identity_namespace = Namespace.COMICVINE
    cv_data.series.external_id = "500"
    cv_data.series.description = "Lower priority core text"
    for index, issue in enumerate(cv_data.issues):
        issue.source = Source.COMICVINE_API
        issue.identity_namespace = Namespace.COMICVINE
        issue.external_id = str(700 + index)
        issue.series_external_id = "500"
    async with factory.begin() as session:
        await attach_verified_identities(
            session,
            [
                _request(
                    cv_data,
                    cv_data.series,
                    ExternalIdentityRef(Namespace.COMICVINE, Kind.SERIES, "500"),
                    series_id,
                ),
                _request(
                    cv_data,
                    cv_data.issues[0],
                    ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "700"),
                    issue_id,
                ),
            ],
        )
        await session.execute(
            update(MetadataSourceConfig)
            .where(MetadataSourceConfig.source == Source.METRON_API.value)
            .values(domain_priorities={"core": 0, "issues": 100})
        )
        await session.execute(
            update(MetadataSourceConfig)
            .where(MetadataSourceConfig.source == Source.COMICVINE_API.value)
            .values(domain_priorities={"core": 100, "issues": 0})
        )
        policies = await read_source_policies(session)
    metron_data.series.description = "Preferred core text"
    adapters = {
        Source.METRON_API: RefreshAdapter(metron_data),
        Source.COMICVINE_API: RefreshAdapter(cv_data),
    }
    adapters[Source.COMICVINE_API].source = Source.COMICVINE_API
    instance = MetadataSourceRegistry(
        [SourceRuntime(policy) for policy in policies if policy.source in adapters],
        factories={
            source: registration(adapter, capabilities=list(SourceCapability))
            for source, adapter in adapters.items()
        },
    )
    async with factory() as session:
        result = await refresh_series_from_sources(session, series_id, registry=instance)
        assert result.description == "Preferred core text"
        assert result.issue_count == 2
        saved = await load_metadata_baseline(session, Kind.SERIES, series_id)
        description = next(item for item in saved.snapshot.origins if item.field == "description")
        assert description.source is Source.METRON_API and description.domain is MetadataDomain.CORE
        await session.commit()
    # One bounded page can supply representative artwork without changing the catalog source.
    assert adapters[Source.METRON_API].calls == [("series", "42"), ("issues", "42", 1)]
    assert adapters[Source.COMICVINE_API].calls == [("series", "500"), ("issues", "500", 1)]


async def test_unknown_issue_crosswalk_remains_observation_not_ownership(identity_probe_db):
    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.models.metadata_identity import IssueIdentityEvent

    _, factory, _ = identity_probe_db
    series_id, _, _ = await seed(factory)
    data = bundle(numbers=("1", "2"))
    data.issues[1].cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "999")]
    async with factory() as session:
        await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data))
        )
        await session.commit()
    async with factory() as session:
        issue = await session.scalar(select(Issue).where(Issue.issue_number_text == "2"))
        assert issue.comicvine_id is None
        event = await session.scalar(
            select(IssueIdentityEvent).where(
                IssueIdentityEvent.identity_namespace == Namespace.COMICVINE
            )
        )
        assert event.verification_state.value == "observed"


@pytest.mark.parametrize(
    "failure", [SourceStatus.RATE_LIMITED, SourceStatus.NOT_FOUND, SourceStatus.TIMEOUT]
)
async def test_failed_profile_is_not_retried_again_for_catalog_in_same_refresh(
    identity_probe_db, failure
):
    _, factory, _ = identity_probe_db
    series_id, _, data = await seed(factory)
    adapter = RefreshAdapter(data)
    adapter.profile_failure = failure
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(adapter)
            )
    assert adapter.calls == [("series", "42")]


@pytest.fixture
async def comicvine_count_disagreement(identity_probe_db):
    from datetime import UTC, datetime

    from pullbox.schemas.metadata_sources import SourceCapability
    from pullbox.services.metadata_discovery import MetadataSourceRegistry
    from tests.unit.test_metadata_discovery import registration, runtime

    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1", "2", "3", "4", "5"))
    generation = datetime(2026, 9, 29, tzinfo=UTC)

    def from_source(source):
        return replace(
            data,
            series=data.series.model_copy(
                update={
                    "source": source,
                    "identity_namespace": Namespace.COMICVINE,
                    "source_updated_at": generation,
                    "description": "Original description",
                }
            ),
            issues=tuple(
                issue.model_copy(
                    update={
                        "source": source,
                        "identity_namespace": Namespace.COMICVINE,
                        "source_updated_at": generation,
                    }
                )
                for issue in data.issues
            ),
        )

    api = from_source(Source.COMICVINE_API)
    local = from_source(Source.COMICVINE_LOCAL)
    async with factory.begin() as session:
        adopted = await adopt_source_series_bundle(session, api)
        series_id = adopted.series.id
        adopted.series.path = "/reference/Civil War - Unmasked"
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        for issue in issues[:4]:
            issue.status = IssueStatus.OWNED
        issues[4].status = IssueStatus.DOWNLOADING
        original = [(i.id, i.comicvine_id, i.issue_number_text, i.status) for i in issues]
    local.series.issue_count = 3
    api.series.description = local.series.description = "Updated description"
    adapters = {
        Source.COMICVINE_LOCAL: RefreshAdapter(local),
        Source.COMICVINE_API: RefreshAdapter(api),
    }
    for source, adapter in adapters.items():
        adapter.source = source
    instance = MetadataSourceRegistry(
        [runtime(source, revision=1) for source in adapters],
        factories={
            source: registration(adapter, capabilities=list(SourceCapability))
            for source, adapter in adapters.items()
        },
    )
    return factory, series_id, original, adapters, instance


@pytest.mark.parametrize("summary_count", [4, 1000001, 1000000000])
async def test_refresh_accepts_complete_catalog_with_stale_summary_without_fallback(
    comicvine_count_disagreement, summary_count
):
    from pullbox.services.metadata_series_refresh import refresh_series_catalog_from_sources

    factory, series_id, original, adapters, instance = comicvine_count_disagreement
    for adapter in adapters.values():
        adapter.data.series.issue_count = summary_count
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=instance)
        assert result.series.description == "Updated description"
        assert result.series.issue_count == 5
        assert result.created_issue_ids == ()
        assert result.series.path == "/reference/Civil War - Unmasked"
        assert result.outcomes == ()
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.issue_number_text, i.status) for i in issues] == original
        assert set(await session.scalars(select(IssueExternalIdentity.identity_namespace))) == {
            Namespace.COMICVINE
        }
    assert not any(call[0] == "issues" for call in adapters[Source.COMICVINE_API].calls)
    assert all(adapter.data.series.issue_count == summary_count for adapter in adapters.values())


@pytest.mark.parametrize("fallback", ["disabled", "rate_limited"])
async def test_changing_page_total_without_consistent_fallback_is_actionable_and_atomic(
    comicvine_count_disagreement, fallback
):
    factory, series_id, original, adapters, instance = comicvine_count_disagreement
    local = adapters[Source.COMICVINE_LOCAL]

    async def changing_pages(external_id, *, page=1, validator=None):
        start, stop = (0, 100) if page == 1 else (100, 102)
        return MetadataFetch(
            status=SourceStatus.OK,
            data=MetadataPage(
                results=[
                    local.data.issues[0].model_copy(
                        update={
                            "external_id": str(1000 + index),
                            "issue_number_text": str(index + 1),
                            "issue_number_key": str(index + 1),
                        }
                    )
                    for index in range(start, stop)
                ],
                total=101 if page == 1 else 102,
                next_page=2 if page == 1 else None,
            ),
        )

    local.issues = changing_pages
    if fallback == "disabled":
        instance.runtime[Source.COMICVINE_API].policy.enabled = False
    else:
        from pullbox.services.metadata_discovery import MetadataSourceError

        async def rate_limited(*args, **kwargs):
            raise MetadataSourceError(SourceStatus.RATE_LIMITED, 300)

        adapters[Source.COMICVINE_API].issues = rate_limited
    async with factory() as session:
        with pytest.raises(SeriesRefreshError, match="inconsistent issue counts"):
            await refresh_series_from_sources(session, series_id, registry=instance)
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).description == "Original description"
        assert (await load_metadata_baseline(session, Kind.SERIES, series_id)).revision == 1
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.issue_number_text, i.status) for i in issues] == original


@pytest.mark.parametrize("conflict", ["wrong_parent", "duplicate_id", "reassigned_issue"])
async def test_actual_catalog_identity_conflict_never_uses_fallback(
    comicvine_count_disagreement, conflict
):
    factory, series_id, original, adapters, instance = comicvine_count_disagreement
    local = adapters[Source.COMICVINE_LOCAL].data
    local.series.issue_count = 5
    if conflict == "wrong_parent":
        local.issues[0].series_external_id = "999"
    elif conflict == "duplicate_id":
        local.issues[0].external_id = local.issues[1].external_id
    else:
        local.issues[0].external_id = "999"
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_from_sources(session, series_id, registry=instance)
        await session.commit()
    assert not any(call[0] == "issues" for call in adapters[Source.COMICVINE_API].calls)
    async with factory() as session:
        assert (await session.get(Series, series_id)).description == "Original description"
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.issue_number_text, i.status) for i in issues] == original
