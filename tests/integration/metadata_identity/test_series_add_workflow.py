"""Real catalog adoption plus filesystem and transaction boundaries on both DBs."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from pullbox.core.events import EventBus, SeriesAdded
from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.models import Issue, Series
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_baseline import IssueMetadataBaseline, SeriesMetadataBaseline
from pullbox.models.metadata_identity import SeriesIdentityEvent
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import SeriesType
from pullbox.schemas.metadata_sources import SourceCapability
from pullbox.services.metadata_discovery import MetadataSourceRegistry, SourceRegistration
from pullbox.services.metadata_series_add import source_series_add_transaction
from pullbox.services.metadata_series_adoption import fetch_source_series_bundle
from pullbox.services.metadata_series_preview import preview_series_folder
from tests.integration.metadata_identity.test_series_adoption import bundle
from tests.unit.test_metadata_discovery import runtime
from tests.unit.test_metron_source import envelope, issue_row, series_row, source


@pytest.fixture
async def add_setup(identity_probe_db, tmp_path, monkeypatch):
    _, factory, _ = identity_probe_db
    root_path = tmp_path / "library"
    root_path.mkdir()
    async with factory.begin() as session:
        session.add(MetadataSourceConfig(source="metron_api", enabled=True, priority=2, revision=1))
        root = LibraryRoot(name="Test library", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        session.add(
            SystemConfig(key="series_folder_template", value="{Publisher}/{Series} ({Year})")
        )
        await session.flush()
        root_id = root.id

    async def covers_dir(_session):
        return tmp_path / "covers"

    monkeypatch.setattr("pullbox.services.cover_cache_service.resolve_covers_dir", covers_dir)
    events = []
    bus = EventBus()

    async def recorded(event):
        async with factory() as session:
            assert await session.get(Series, event.series_id) is not None
        events.append(event)

    bus.subscribe(SeriesAdded, recorded)
    return factory, root_id, root_path, bus, events


def titled_bundle(title="Source series", *, publisher="Fixture Press", **updates):
    result = bundle()
    return replace(
        result,
        series=result.series.model_copy(
            update={
                "title": title,
                "sort_title": title,
                "year_start": 2024,
                "publisher": publisher,
                **updates,
            }
        ),
    )


@pytest.mark.parametrize("source", [Source.COMICVINE_LOCAL, Source.METRON_API, Source.GCD_LOCAL])
@pytest.mark.parametrize("issue_count", [None, 12])
async def test_preview_renders_shared_naming_without_adopting_profile(
    add_setup, source, issue_count
):
    from pullbox.models.publisher import Publisher

    factory, root_id, root_path, _bus, events = add_setup
    profile = titled_bundle().series.model_copy(
        update={
            "source": source,
            "identity_namespace": source.identity_namespace,
            "issue_count": issue_count,
        }
    )
    async with factory() as session:
        root = await session.get(LibraryRoot, root_id)
        assert await preview_series_folder(session, profile, root) == (
            "Fixture Press/Source series (2024)"
        )
        assert not session.new and not session.dirty
        for model in (Series, Issue, Publisher, SeriesIdentityEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert list(root_path.iterdir()) == [] and not events


async def test_http_registry_to_committed_series_and_event_without_comicvine(add_setup):
    factory, root_id, root_path, bus, events = add_setup
    requests = []
    async with factory() as session:

        def handle(request):
            assert not session.in_transaction(), "Provider I/O must not hold a DB transaction"
            requests.append(request)
            if request.url.path == "/api/series/8/":
                return httpx.Response(
                    200, json={**series_row(8), "name": "Fixture 8", "cv_id": None, "gcd_id": None}
                )
            assert request.url.path == "/api/series/8/issue_list/"
            return httpx.Response(
                200,
                json=envelope(
                    [
                        issue_row(100, "13a"),
                        issue_row(101, "13b"),
                        issue_row(102, "50-x"),
                    ]
                ),
            )

        registry = MetadataSourceRegistry(
            [runtime(Source.METRON_API, revision=1)],
            factories={
                Source.METRON_API: SourceRegistration(
                    frozenset(
                        {
                            SourceCapability.SERIES_DETAILS,
                            SourceCapability.ISSUE_LIST,
                        }
                    ),
                    lambda _: source(handle),
                ),
            },
        )
        fetched = await fetch_source_series_bundle(
            registry, Source.METRON_API, "8", source_revision=1
        )
        async with source_series_add_transaction(
            session, fetched, library_root_id=root_id, search_on_add=True, event_bus=bus
        ) as result:
            assert result.created
            series_id = result.series.id
            assert not events
            assert result.series.path == str(root_path / "Fixture Press/Fixture 8 (2024)")
            assert result.series.comicvine_id is None
        assert not session.in_transaction()
    assert len(requests) == 2 and len(events) == 1
    assert events[0].series_id == series_id and events[0].comicvine_id is None
    async with factory() as session:
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [issue.issue_number_text for issue in issues] == ["13A", "13B", "50-X"]
        assert all(issue.status.value == "wanted" for issue in issues)


@pytest.mark.parametrize("failure", ["response", "commit", "cancel"])
async def test_failure_removes_only_created_directories_and_no_event(
    add_setup, monkeypatch, failure
):
    factory, root_id, root_path, bus, events = add_setup
    publisher = root_path / "Fixture Press"
    publisher.mkdir()
    marker = publisher / "unrelated.cbz"
    marker.write_bytes(b"untouched")
    async with factory() as session:
        if failure == "commit":
            from sqlalchemy import event

            def reject_commit(sync_session):
                if not sync_session.in_nested_transaction():
                    raise RuntimeError("commit failed")

            event.listen(session.sync_session, "before_commit", reject_commit)
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            async with source_series_add_transaction(
                session,
                titled_bundle(),
                library_root_id=root_id,
                search_on_add=False,
                event_bus=bus,
            ) as result:
                assert result.series.path is not None
                assert Path(result.series.path).is_dir()
                if failure == "cancel":
                    raise asyncio.CancelledError
                if failure == "response":
                    raise RuntimeError("response failed")
    assert list(publisher.iterdir()) == [marker] and marker.read_bytes() == b"untouched"
    assert not events
    async with factory() as session:
        for model in (
            Series,
            Issue,
            SeriesIdentityEvent,
            SeriesMetadataBaseline,
            IssueMetadataBaseline,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_existing_owner_is_not_moved_or_remonitored(add_setup):
    factory, root_id, root_path, bus, events = add_setup
    async with factory() as session:
        async with source_series_add_transaction(
            session, titled_bundle(), library_root_id=None, search_on_add=False, event_bus=bus
        ) as result:
            series_id = result.series.id
        async with session.begin():
            result.series.path = "/existing/reference/keep"
            result.series.title = "Operator title"
        async with source_series_add_transaction(
            session, titled_bundle(), library_root_id=root_id, search_on_add=True, event_bus=bus
        ) as result:
            assert not result.created and result.series.id == series_id
            assert result.series.title == "Operator title" and not result.series.monitored
            assert result.series.path == "/existing/reference/keep"
    assert len(events) == 1 and not list(root_path.iterdir())
    async with factory() as session:
        baseline = await session.scalar(select(SeriesMetadataBaseline))
        assert baseline.revision == 1
        assert "Operator title" not in baseline.snapshot_json


async def test_initial_sidecar_uses_committed_catalog_and_only_the_new_collision_folder(add_setup):
    from pullbox.services.series_sidecar import create_added_series_sidecar

    factory, root_id, root_path, bus, _ = add_setup
    old = root_path / "Fixture Press/Source series (2024)"
    old.mkdir(parents=True)
    original = old / "series.json"
    original.write_text('{"custom":"keep"}')
    async with factory() as session:
        async with source_series_add_transaction(
            session, titled_bundle(), library_root_id=root_id, search_on_add=False, event_bus=bus
        ) as result:
            folder = Path(result.series.path)
            assert result.created_folder.folder_path == folder != old
            assert not (folder / "series.json").exists()
        assert await create_added_series_sidecar(session, result) == "created"
    assert original.read_text() == '{"custom":"keep"}'
    snapshot = json.loads((folder / "series.json").read_bytes())["pullbox"]["snapshot"]
    assert snapshot["values"]["title"] == "Source series"


async def test_final_classification_is_reflected_in_the_committed_baseline(add_setup):
    from pullbox.core.metadata_identity import MetadataEntityKind
    from pullbox.services.metadata_baselines import load_metadata_baseline

    factory, _, _, bus, _ = add_setup
    data = titled_bundle()
    for issue in data.issues:
        issue.title = "Annual"
    async with (
        factory() as session,
        source_series_add_transaction(
            session, data, library_root_id=None, search_on_add=False, event_bus=bus
        ) as result,
    ):
        local_id = result.series.id
        assert result.series.series_type is SeriesType.ANNUAL
        assert result.snapshot.values.series_type == "annual"
    async with factory() as session:
        baseline = await load_metadata_baseline(session, MetadataEntityKind.SERIES, local_id)
        assert baseline.snapshot == result.snapshot
        assert baseline.snapshot.values.series_type == "annual"


async def test_existing_unregistered_folders_are_preserved_with_native_suffix(add_setup):
    factory, root_id, root_path, bus, events = add_setup
    existing = root_path / "Fixture Press/Source series (2024)"
    existing.mkdir(parents=True)
    async with (
        factory() as session,
        source_series_add_transaction(
            session, titled_bundle(), library_root_id=root_id, search_on_add=False, event_bus=bus
        ) as result,
    ):
        assert result.series.path == str(existing.with_name(existing.name + " [metron-42]"))
    assert existing.is_dir() and len(events) == 1


async def test_taken_native_suffix_does_not_merge_into_unowned_directory(add_setup):
    factory, root_id, root_path, bus, events = add_setup
    existing = root_path / "Fixture Press/Source series (2024)"
    existing.mkdir(parents=True)
    collision = existing.with_name(existing.name + " [metron-42]")
    collision.mkdir()
    async with factory() as session:
        with pytest.raises(ValidationError):
            async with source_series_add_transaction(
                session,
                titled_bundle(),
                library_root_id=root_id,
                search_on_add=False,
                event_bus=bus,
            ):
                pass
    assert existing.is_dir() and collision.is_dir() and not events


@pytest.mark.parametrize("flag", ["enabled", "allow_managed_writes"])
async def test_root_policy_rejection_does_not_leave_catalog(add_setup, flag):
    factory, root_id, root_path, bus, events = add_setup
    async with factory.begin() as session:
        root = await session.get(LibraryRoot, root_id)
        setattr(root, flag, False)
    async with factory() as session:
        with pytest.raises(ValidationError):
            async with source_series_add_transaction(
                session,
                titled_bundle(),
                library_root_id=root_id,
                search_on_add=False,
                event_bus=bus,
            ):
                pass
    assert not list(root_path.iterdir()) and not events
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


async def test_active_caller_transaction_is_never_committed(add_setup):
    factory, root_id, _, bus, events = add_setup
    async with factory() as session:
        await session.get(LibraryRoot, root_id)
        with pytest.raises(ValueError, match="transaction"):
            async with source_series_add_transaction(
                session,
                titled_bundle(),
                library_root_id=root_id,
                search_on_add=False,
                event_bus=bus,
            ):
                pass
        assert session.in_transaction() and not events


async def test_complete_collection_consensus_and_parent_linking_precede_folder_naming(add_setup):
    factory, root_id, _, bus, _ = add_setup
    async with factory.begin() as session:
        parent = Series(title="Source series", sort_title="Source series", year_start=2024)
        session.add(parent)
        await session.flush()
        parent_id = parent.id
    data = titled_bundle("Source series Annual", series_type="annual")
    async with (
        factory() as session,
        source_series_add_transaction(
            session, data, library_root_id=root_id, search_on_add=False, event_bus=bus
        ) as result,
    ):
        assert result.series.parent_series_id == parent_id
        assert result.series.series_type is SeriesType.ANNUAL
    data = titled_bundle()
    data = replace(
        data, issues=tuple(issue.model_copy(update={"title": "Omnibus"}) for issue in data.issues)
    )
    async with factory() as session:
        # A different source series, not a refresh of the annual above.
        data = replace(
            data,
            series=data.series.model_copy(update={"external_id": "99"}),
            issues=tuple(
                issue.model_copy(update={"external_id": str(500 + i), "series_external_id": "99"})
                for i, issue in enumerate(data.issues)
            ),
        )
        async with source_series_add_transaction(
            session, data, library_root_id=None, search_on_add=False, event_bus=bus
        ) as result:
            assert result.series.series_type is SeriesType.OMNIBUS
