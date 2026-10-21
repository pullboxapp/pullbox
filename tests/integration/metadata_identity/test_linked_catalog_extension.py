"""Linked catalogs extend proven membership without replacing library identity."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from pullbox.core.metadata_identity import ExternalIdentityRef
from pullbox.core.metadata_identity import IdentityNamespace as Namespace
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.core.metadata_identity_state import IdentityVerificationAction as Action
from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.metadata_identity import (
    IssueExternalIdentity,
    IssueIdentityEvent,
    SeriesExternalIdentity,
)
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.reader import IssueReaderState
from pullbox.models.user import User
from pullbox.schemas.metadata_sources import MetadataFetch, SourceCapability, SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_identity_attachment import attach_verified_identities
from pullbox.services.metadata_identity_review import record_identity_observation
from pullbox.services.metadata_series_adoption import _request, adopt_source_series_bundle
from pullbox.services.metadata_series_refresh import (
    SeriesRefreshError,
    refresh_series_catalog_from_sources,
)
from tests.integration.metadata_identity.test_identity_review import decide
from tests.integration.metadata_identity.test_series_adoption import (  # noqa: F401
    bundle,
    configured_sources,
)
from tests.integration.metadata_identity.test_series_refresh import RefreshAdapter
from tests.unit.test_metadata_discovery import registration, runtime


@pytest.fixture
async def linked_catalog(identity_probe_db, tmp_path, request):
    _, factory, _ = identity_probe_db
    existing_count = getattr(request, "param", 5)
    metron = bundle(numbers=tuple(str(i) for i in range(1, 8)))
    metron.series.cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.SERIES, "500")]
    metron.series.description = "Secondary description"
    for index, issue in enumerate(metron.issues):
        issue.external_id = str(200 + index)
        issue.title = f"Secondary issue {index + 1}"
        if index < existing_count:
            issue.cross_identities = [
                ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, str(100 + index))
            ]
    metron.issues[-1].store_date = date.today() + timedelta(days=30)
    cv = replace(
        metron,
        series=metron.series.model_copy(
            update={
                "source": Source.COMICVINE_API,
                "identity_namespace": Namespace.COMICVINE,
                "external_id": "500",
                "issue_count": existing_count,
                "cross_identities": [],
                "description": "Preferred description",
            }
        ),
        issues=tuple(
            issue.model_copy(
                update={
                    "source": Source.COMICVINE_API,
                    "identity_namespace": Namespace.COMICVINE,
                    "series_external_id": "500",
                    "external_id": str(100 + index),
                    "cross_identities": [],
                    "title": f"Preferred issue {index + 1}",
                }
            )
            for index, issue in enumerate(metron.issues[:existing_count])
        ),
        catalog_total=existing_count,
    )
    artifact = tmp_path / "owned.cbz"
    artifact.write_bytes(b"owned comic must remain untouched")
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, cv, monitored=True)
        series_id = result.series.id
        result.series.path = str(tmp_path)
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        issues[0].status = IssueStatus.OWNED
        issues[1].title = "My issue title"
        issues[2].status = IssueStatus.SKIPPED
        issues[2].manual_skip = True
        original = [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues]
        root = LibraryRoot(name="Reference", path=str(tmp_path))
        user = User(username="catalog-reader", password_hash="not-a-live-account")
        session.add_all([root, user])
        await session.flush()
        session.add(
            LibraryFile(
                issue_id=issues[0].id,
                library_root_id=root.id,
                file_path=str(artifact),
                file_name=artifact.name,
                file_format="cbz",
                file_size=artifact.stat().st_size,
                file_modified_at=datetime.now(UTC),
                storage_mode="referenced",
            )
        )
        session.add(
            IssueReaderState(
                user_id=user.id,
                issue_id=issues[0].id,
                last_page_index=17,
                page_count=24,
            )
        )
        await attach_verified_identities(
            session,
            [
                _request(
                    metron,
                    metron.series,
                    ExternalIdentityRef(Namespace.METRON, Kind.SERIES, "42"),
                    series_id,
                )
            ],
        )
    adapters = {Source.COMICVINE_API: RefreshAdapter(cv), Source.METRON_API: RefreshAdapter(metron)}
    for source, adapter in adapters.items():
        adapter.source = source
    registry = MetadataSourceRegistry(
        [runtime(source, revision=1, priority=index) for index, source in enumerate(adapters, 1)],
        factories={
            source: registration(adapter, capabilities=list(SourceCapability))
            for source, adapter in adapters.items()
        },
    )
    return factory, series_id, original, artifact, adapters, registry


class ThinMetronCatalog(RefreshAdapter):
    """Metron's list omits crosswalks that its native issue details provide."""

    def __init__(self, data):
        super().__init__(
            replace(
                data,
                issues=tuple(i.model_copy(update={"cross_identities": []}) for i in data.issues),
            )
        )
        self.details = {i.external_id: i.model_copy(deep=True) for i in data.issues}
        self.detail_failure = None

    async def issue(self, external_id, *, validator=None):
        self.calls.append(("issue", external_id))
        if self.detail_failure:
            raise MetadataSourceError(self.detail_failure, retry_after_seconds=60)
        return MetadataFetch(status=SourceStatus.OK, data=self.details[external_id])


@pytest.mark.parametrize("linked_catalog", [6], indirect=True)
async def test_thin_linked_catalog_adds_seventh_issue_using_native_detail_proof(linked_catalog):
    factory, series_id, original, artifact, adapters, registry = linked_catalog
    adapter = ThinMetronCatalog(adapters[Source.METRON_API].data)
    registry.factories = {
        **registry.factories,
        Source.METRON_API: registration(adapter, capabilities=list(SourceCapability)),
    }
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.series.issue_count == 7
        assert len(result.created_issue_ids) == 1
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues[:6]] == original
        assert issues[1].title == "My issue title"
        assert issues[-1].issue_number_text == "7" and issues[-1].comicvine_id is None
        assert issues[-1].store_date == adapter.details["206"].store_date
        assert (await session.scalar(select(IssueReaderState))).last_page_index == 17
        assert (await session.scalar(select(LibraryFile))).file_path == str(artifact)
    assert [c for c in adapter.calls if c[0] == "issue"] == [
        ("issue", str(200 + i)) for i in range(6)
    ]
    adapter.calls.clear()
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.created_issue_ids == () and result.series.issue_count == 7
        await session.commit()
    assert not [c for c in adapter.calls if c[0] == "issue"]
    assert artifact.read_bytes() == b"owned comic must remain untouched"


async def test_thin_catalog_checks_overlap_added_by_another_catalog_in_the_same_refresh(
    linked_catalog,
):
    factory, series_id, original, _, adapters, registry = linked_catalog
    metron = adapters[Source.METRON_API].data
    metron.issues[5].cross_identities = [
        ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "105")
    ]
    cv = adapters[Source.COMICVINE_API]
    cv.data = replace(
        cv.data,
        catalog_total=6,
        issues=(
            *cv.data.issues,
            metron.issues[5].model_copy(
                update={
                    "source": Source.COMICVINE_API,
                    "identity_namespace": Namespace.COMICVINE,
                    "series_external_id": "500",
                    "external_id": "105",
                    "cross_identities": [],
                }
            ),
        ),
    )
    adapter = ThinMetronCatalog(metron)
    registry.factories = {
        **registry.factories,
        Source.METRON_API: registration(adapter, capabilities=list(SourceCapability)),
    }
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.series.issue_count == 7 and len(result.created_issue_ids) == 2
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues[:5]] == original
        assert issues[5].comicvine_id == 105
        assert issues[6].comicvine_id is None
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 13
    assert [c for c in adapter.calls if c[0] == "issue"] == [
        ("issue", str(200 + i)) for i in range(6)
    ]


@pytest.mark.parametrize(
    "problem",
    ["number_only", "wrong_parent", "wrong_id", "renumbered", "wrong_crosswalk", "rate_limited"],
)
async def test_thin_catalog_requires_consistent_detail_identity_and_keeps_atomicity(
    linked_catalog, problem
):
    factory, series_id, original, artifact, adapters, registry = linked_catalog
    adapter = ThinMetronCatalog(adapters[Source.METRON_API].data)
    detail = adapter.details["200"]
    if problem == "number_only":
        detail.cross_identities = []
    elif problem == "wrong_parent":
        detail.series_external_id = "999"
    elif problem == "wrong_id":
        detail.external_id = "999"
    elif problem == "renumbered":
        detail.issue_number_text = detail.issue_number_key = "99"
    elif problem == "wrong_crosswalk":
        detail.cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "999")]
    else:
        adapter.detail_failure = SourceStatus.RATE_LIMITED
    registry.factories = {
        **registry.factories,
        Source.METRON_API: registration(adapter, capabilities=list(SourceCapability)),
    }
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).issue_count == 5
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues] == original
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 5
    if problem == "rate_limited":
        assert [c for c in adapter.calls if c[0] == "issue"] == [("issue", "200")]
    assert artifact.read_bytes() == b"owned comic must remain untouched"


async def test_thin_catalog_detail_budget_is_checked_before_requests(linked_catalog):
    factory, series_id, original, _, adapters, registry = linked_catalog
    data = adapters[Source.METRON_API].data
    issues = tuple(
        data.issues[0].model_copy(
            update={
                "external_id": str(200 + i),
                "issue_number_text": str(i + 1),
                "issue_number_key": str(i + 1),
                "cross_identities": [],
            }
        )
        for i in range(26)
    )
    adapter = ThinMetronCatalog(replace(data, issues=issues, catalog_total=26))
    cv = adapters[Source.COMICVINE_API]
    cv.data = replace(
        cv.data,
        issues=tuple(
            issue.model_copy(
                update={
                    "source": Source.COMICVINE_API,
                    "identity_namespace": Namespace.COMICVINE,
                    "series_external_id": "500",
                    "external_id": str(100 + i),
                }
            )
            for i, issue in enumerate(issues)
        ),
        catalog_total=26,
    )
    registry.factories = {
        **registry.factories,
        Source.METRON_API: registration(adapter, capabilities=list(SourceCapability)),
    }
    async with factory() as session:
        with pytest.raises(SeriesRefreshError, match="more issue identity checks"):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    assert not [c for c in adapter.calls if c[0] == "issue"]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == len(original)


async def test_linked_catalog_is_checked_even_when_preferred_profile_fills_every_field(
    linked_catalog,
):
    factory, series_id, _, _, adapters, registry = linked_catalog
    profile = adapters[Source.COMICVINE_API].data.series
    profile.sort_title = profile.title
    profile.publisher = "DC"
    profile.year_start = 2025
    profile.year_end = 2026
    profile.series_type = "standard"
    profile.status = "ended"
    profile.image_url = "https://comicvine.gamespot.com/cover.jpg"
    profile.volume = "1"
    profile.language = "en"
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.series.issue_count == 7, "Catalog extension cannot depend on missing fields"
        assert len(result.created_issue_ids) == 2
    assert ("series", "42") in adapters[Source.METRON_API].calls
    assert ("issues", "42", 1) in adapters[Source.METRON_API].calls


@pytest.mark.parametrize("replace_managed", [False, True])
async def test_linked_catalog_extends_five_to_seven_preserving_existing_state(
    linked_catalog, replace_managed
):
    factory, series_id, original, artifact, adapters, registry = linked_catalog
    before = artifact.stat()
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(
            session, series_id, registry=registry, replace_managed=replace_managed
        )
        assert result.series.issue_count == 7, (
            "The linked catalog must supply the two missing issues"
        )
        assert len(result.created_issue_ids) == 2
        assert result.series.description == "Preferred description"
        assert result.series.path == str(artifact.parent)
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues[:5]] == original
        assert issues[0].title == "Preferred issue 1"
        assert issues[1].title == "My issue title"
        assert [i.issue_number_text for i in issues] == [str(i) for i in range(1, 8)]
        assert issues[-1].store_date == adapters[Source.METRON_API].data.issues[-1].store_date
        assert all(i.status is IssueStatus.WANTED for i in issues[5:])
        assert all(i.comicvine_id is None for i in issues[5:])
        claims = list(await session.scalars(select(IssueExternalIdentity)))
        assert len(claims) == 12
        assert all(c.verification_state.value == "verified" for c in claims)
        reader = await session.scalar(select(IssueReaderState))
        assert reader.last_page_index == 17 and reader.page_count == 24
        assert (await session.scalar(select(LibraryFile))).file_path == str(artifact)
        events = await session.scalar(select(func.count()).select_from(IssueIdentityEvent))
    assert artifact.read_bytes() == b"owned comic must remain untouched"
    assert (artifact.stat().st_ino, artifact.stat().st_mtime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
    )
    async with factory() as session:
        repeated = await refresh_series_catalog_from_sources(
            session, series_id, registry=registry, replace_managed=replace_managed
        )
        assert repeated.created_issue_ids == () and repeated.series.issue_count == 7
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == 7
        assert await session.scalar(select(func.count()).select_from(IssueIdentityEvent)) == events


@pytest.mark.parametrize(
    "problem", ["number_only", "wrong_parent", "renumbered", "wrong_crosswalk"]
)
async def test_linked_catalog_conflict_is_atomic_and_does_not_reassign(linked_catalog, problem):
    factory, series_id, original, artifact, adapters, registry = linked_catalog
    data = adapters[Source.METRON_API].data
    if problem == "number_only":
        data.issues[0].cross_identities = []
    elif problem == "wrong_parent":
        data.issues[0].series_external_id = "999"
    elif problem == "renumbered":
        data.issues[0].issue_number_text = "99"
        data.issues[0].issue_number_key = "99"
    else:
        data.issues[0].cross_identities = [
            ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "999")
        ]
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).issue_count == 5
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues] == original
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 5
    assert artifact.read_bytes() == b"owned comic must remain untouched"


async def test_linked_catalog_cannot_reactivate_a_rejected_overlap(linked_catalog):
    factory, series_id, original, artifact, adapters, registry = linked_catalog
    data = adapters[Source.METRON_API].data
    issue_id = original[0][0]
    async with factory.begin() as session:
        user_id = await session.scalar(select(User.id))
        saved = await record_identity_observation(
            session,
            _request(
                data,
                data.issues[0],
                ExternalIdentityRef(Namespace.METRON, Kind.ISSUE, "200"),
                issue_id,
                observation=True,
            ),
        )
        await decide(session, Kind.ISSUE, issue_id, saved.event_id, user_id, Action.REJECT)
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).issue_count == 5
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 5
        event = await session.scalar(
            select(IssueIdentityEvent).order_by(IssueIdentityEvent.id.desc())
        )
        assert event.verification_state.value == "rejected"
    assert artifact.read_bytes() == b"owned comic must remain untouched"


async def test_linked_catalog_preserves_priorities_when_supplement_is_first(linked_catalog):
    factory, series_id, original, _, _, registry = linked_catalog
    async with factory.begin() as session:
        for source, priority in ((Source.METRON_API, 0), (Source.COMICVINE_API, 10)):
            priorities = {"core": priority, "issues": priority}
            await session.execute(
                update(MetadataSourceConfig)
                .where(MetadataSourceConfig.source == source.value)
                .values(domain_priorities=priorities)
            )
            registry.runtime[source] = runtime(source, revision=1, domain_priorities=priorities)
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(
            session, series_id, registry=registry, replace_managed=True
        )
        assert result.series.issue_count == 7 and len(result.created_issue_ids) == 2
        assert result.series.description == "Secondary description"
        await session.commit()
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert issues[0].title == "Secondary issue 1"
        assert issues[1].title == "My issue title"
        assert [(i.id, i.comicvine_id, i.status, i.manual_skip) for i in issues[:5]] == original


async def test_incomplete_supplement_cannot_partially_extend_catalog(linked_catalog, monkeypatch):
    factory, series_id, _, _, adapters, registry = linked_catalog
    adapter = adapters[Source.METRON_API]
    original_issues = adapter.issues

    async def missing_member(external_id, *, page=1, validator=None):
        result = await original_issues(external_id, page=page, validator=validator)
        result.data.results.pop()
        return result

    monkeypatch.setattr(adapter, "issues", missing_member)
    async with factory() as session:
        with pytest.raises(SeriesRefreshError):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    async with factory() as session:
        assert (await session.get(Series, series_id)).issue_count == 5
        assert await session.scalar(select(func.count()).select_from(Issue)) == 5
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 5


async def test_linked_catalog_extension_respects_paused_series(linked_catalog):
    factory, series_id, _, _, _, registry = linked_catalog
    async with factory.begin() as session:
        (await session.get(Series, series_id)).monitored = False
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert len(result.created_issue_ids) == 2
        await session.commit()
    async with factory() as session:
        added = list(
            await session.scalars(select(Issue).where(Issue.id.in_(result.created_issue_ids)))
        )
        assert all(issue.status is IssueStatus.SKIPPED for issue in added)


async def test_unverified_series_link_cannot_extend_catalog(linked_catalog):
    factory, series_id, _, _, adapters, registry = linked_catalog
    async with factory.begin() as session:
        claim = await session.scalar(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.identity_namespace == Namespace.METRON,
            )
        )
        claim.verification_state = "stale"
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.created_issue_ids == () and result.series.issue_count == 5
        await session.commit()
    assert adapters[Source.METRON_API].calls == []


async def test_later_linked_catalog_cannot_drop_a_previously_verified_member(linked_catalog):
    factory, series_id, _, artifact, adapters, registry = linked_catalog
    async with factory() as session:
        result = await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        assert result.series.issue_count == 7
        await session.commit()
    data = adapters[Source.METRON_API].data
    adapters[Source.METRON_API].data = replace(data, issues=data.issues[:-1], catalog_total=6)
    async with factory() as session:
        with pytest.raises(SeriesRefreshError, match="removed existing issue identities"):
            await refresh_series_catalog_from_sources(session, series_id, registry=registry)
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == 7
    assert artifact.read_bytes() == b"owned comic must remain untouched"
