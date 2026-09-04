"""Unit tests for background catalog hydration helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.models.import_job import (
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot
from pullbox.models.operation_progress import OperationProgress, OperationProgressState
from pullbox.models.series import IssueCatalogState, Series
from pullbox.providers.base import IssueSummary, SeriesMetadata
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider
from pullbox.services.import_catalog_hydration import (
    load_catalog_hydration_plan,
    mark_catalog_hydration_failed,
    run_catalog_hydration,
    run_pending_catalog_hydration,
)
from pullbox.services.metadata_service import MetadataService
from pullbox.services.series_service import SeriesService


class CatalogHydrationSeriesServiceStub:
    def __init__(self, *, failing_cv_ids: set[int] | None = None) -> None:
        self.failing_cv_ids = failing_cv_ids or set()
        self.prefetch_calls: list[int] = []
        self.add_calls: list[tuple[int, bool]] = []

    async def prefetch_comicvine_bundle(
        self,
        comicvine_id: int,
    ) -> tuple[dict[str, int], list[Any]]:
        self.prefetch_calls.append(comicvine_id)
        if comicvine_id in self.failing_cv_ids:
            raise RuntimeError(f"ComicVine failed for {comicvine_id}")
        return {"comicvine_id": comicvine_id}, []

    async def add_from_comicvine_prefetched(
        self,
        hydrate_session,
        *,
        comicvine_id: int,
        library_root_id: int | None,
        search_on_add: bool,
        series_meta,
        issue_summaries,
    ) -> Series:
        _ = library_root_id, series_meta, issue_summaries
        self.add_calls.append((comicvine_id, search_on_add))
        result = await hydrate_session.execute(
            sa_select(Series).where(Series.comicvine_id == comicvine_id)
        )
        series = result.scalar_one()
        series.issue_catalog_state = IssueCatalogState.COMPLETE
        series.issue_catalog_error = None
        await hydrate_session.flush()
        return series


class CatalogHydrationProviderDouble:
    name = "comicvine"

    def __init__(self, *, fail_on_fetch: bool = False) -> None:
        self.fail_on_fetch = fail_on_fetch
        self.series_calls = 0
        self.issue_list_calls = 0

    async def get_series(self, series_provider_id: str) -> SeriesMetadata:
        self.series_calls += 1
        if self.fail_on_fetch:
            raise AssertionError("hydration should reuse cached series metadata")
        return SeriesMetadata(
            provider_id=str(series_provider_id),
            title="Cached Hydration",
            sort_title="Cached Hydration",
            year_start=2026,
            year_end=None,
            status=None,
            publisher="Pullbox",
            description="Cached in Step 2",
            cover_url=None,
            issue_count=1,
            comicvine_url=f"https://comicvine.gamespot.com/cached/{series_provider_id}/",
        )

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        self.issue_list_calls += 1
        if self.fail_on_fetch:
            raise AssertionError("hydration should reuse cached issue summaries")
        return [
            IssueSummary(
                provider_id=f"{series_provider_id}001",
                issue_number=1.0,
                title="Cached Issue",
                release_date="2026-01-01",
                cover_url=None,
                issue_type="issue",
            )
        ]


class BatchCatalogHydrationSeriesServiceStub(CatalogHydrationSeriesServiceStub):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.profile_batch_calls: list[list[int]] = []
        self.catalog_batch_calls: list[list[int]] = []

    async def prefetch_comicvine_profiles(self, comicvine_ids: list[int]):
        self.profile_batch_calls.append(list(comicvine_ids))
        self.events.append("profiles_fetched")
        return {
            comicvine_id: {"title": f"Hydrated {comicvine_id}"} for comicvine_id in comicvine_ids
        }

    async def prefetch_comicvine_issue_catalogs(self, comicvine_ids: list[int]):
        assert self.events.count("profile_persisted") == len(comicvine_ids)
        self.catalog_batch_calls.append(list(comicvine_ids))
        self.events.append("catalogs_fetched")
        return {comicvine_id: [] for comicvine_id in comicvine_ids}

    async def upsert_comicvine_profile(self, hydrate_session, comicvine_id: int, series_meta):
        result = await hydrate_session.execute(
            sa_select(Series).where(Series.comicvine_id == comicvine_id)
        )
        series = result.scalar_one()
        series.title = series_meta["title"]
        self.events.append("profile_persisted")
        await hydrate_session.flush()
        return series


async def test_run_pending_catalog_hydration_recovers_hydrating_series_after_restart(
    db_session,
) -> None:
    first = Series(
        title="First Restart Recovery",
        sort_title="first restart recovery",
        year_start=2026,
        comicvine_id=1001,
        monitored=True,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    already_complete = Series(
        title="Already Complete",
        sort_title="already complete",
        year_start=2026,
        comicvine_id=2001,
        issue_catalog_state=IssueCatalogState.COMPLETE,
    )
    second = Series(
        title="Second Restart Recovery",
        sort_title="second restart recovery",
        year_start=2026,
        comicvine_id=1002,
        monitored=False,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    no_provider_id = Series(
        title="No Provider ID",
        sort_title="no provider id",
        year_start=2026,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add_all([first, already_complete, second, no_provider_id])
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    service = CatalogHydrationSeriesServiceStub()

    recovered = await run_pending_catalog_hydration(
        session_factory,
        series_service=service,
    )

    assert recovered == 2
    assert service.prefetch_calls == [1001, 1002]
    assert service.add_calls == [(1001, True), (1002, False)]
    for series in (first, second):
        await db_session.refresh(series)
        assert series.issue_catalog_state == IssueCatalogState.COMPLETE
        assert series.issue_catalog_error is None
    await db_session.refresh(already_complete)
    await db_session.refresh(no_provider_id)
    assert already_complete.issue_catalog_state == IssueCatalogState.COMPLETE
    assert no_provider_id.issue_catalog_state == IssueCatalogState.HYDRATING


async def test_run_pending_catalog_hydration_batches_profiles_before_issue_catalogs(
    db_session,
) -> None:
    series_rows = [
        Series(
            title=f"Pending {comicvine_id}",
            sort_title=f"pending {comicvine_id}",
            comicvine_id=comicvine_id,
            issue_catalog_state=IssueCatalogState.HYDRATING,
        )
        for comicvine_id in (6101, 6102, 6103)
    ]
    db_session.add_all(series_rows)
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    service = BatchCatalogHydrationSeriesServiceStub()

    recovered = await run_pending_catalog_hydration(
        session_factory,
        series_service=service,
    )

    assert recovered == 3
    assert service.profile_batch_calls == [[6101, 6102, 6103]]
    assert service.catalog_batch_calls == [[6101, 6102, 6103]]
    assert service.prefetch_calls == []
    assert service.events.index("profiles_fetched") < service.events.index("profile_persisted")
    assert service.events.index("profile_persisted") < service.events.index("catalogs_fetched")
    for series in series_rows:
        await db_session.refresh(series)
        assert series.title == f"Hydrated {series.comicvine_id}"
        assert series.issue_catalog_state == IssueCatalogState.COMPLETE


async def test_catalog_hydration_does_not_recreate_series_deleted_during_fetch(
    db_session,
) -> None:
    series = Series(
        title="Rollback Race",
        sort_title="rollback race",
        comicvine_id=4040,
        monitored=True,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add(series)
    await db_session.commit()
    series_id = series.id
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    add_calls = 0

    async def delete_while_fetching(_comicvine_id: int):
        async with session_factory() as delete_session:
            persisted = await delete_session.get(Series, series_id)
            assert persisted is not None
            await delete_session.delete(persisted)
            await delete_session.commit()
        return {"comicvine_id": 4040}, []

    async def add_from_prefetched(*_args, **_kwargs):
        nonlocal add_calls
        add_calls += 1

    hydrated = await run_catalog_hydration(
        session_factory,
        series_id=series_id,
        search_on_add=True,
        prefetch_comicvine_bundle=delete_while_fetching,
        add_from_comicvine_prefetched=add_from_prefetched,
    )

    assert add_calls == 0
    assert hydrated is False


async def test_catalog_hydration_skips_series_completed_after_queue_snapshot(db_session) -> None:
    series = Series(
        title="Completed While Queued",
        sort_title="completed while queued",
        comicvine_id=4041,
        monitored=True,
        issue_catalog_state=IssueCatalogState.COMPLETE,
    )
    db_session.add(series)
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    prefetch = AsyncMock()
    persist = AsyncMock()

    hydrated = await run_catalog_hydration(
        session_factory,
        series_id=series.id,
        search_on_add=True,
        prefetch_comicvine_bundle=prefetch,
        add_from_comicvine_prefetched=persist,
    )

    assert hydrated is False
    prefetch.assert_not_awaited()
    persist.assert_not_awaited()


async def test_load_catalog_hydration_plan_uses_explicit_preferred_root(db_session) -> None:
    current_root = LibraryRoot(
        name="Existing",
        path="/existing",
        enabled=True,
        allow_managed_writes=False,
    )
    preferred_root = LibraryRoot(name="Future", path="/future", enabled=True)
    db_session.add_all([current_root, preferred_root])
    await db_session.flush()
    series = Series(
        title="Preferred Hydration",
        sort_title="preferred hydration",
        comicvine_id=4010,
        library_root_id=current_root.id,
        preferred_library_root_id=preferred_root.id,
    )
    db_session.add(series)
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )

    plan = await load_catalog_hydration_plan(
        session_factory,
        series_id=series.id,
        search_on_add=False,
    )

    assert plan is not None
    assert plan.library_root_id == preferred_root.id


async def test_load_catalog_hydration_plan_does_not_force_reference_only_current_root(
    db_session,
) -> None:
    current_root = LibraryRoot(
        name="Existing",
        path="/existing",
        enabled=True,
        allow_managed_writes=False,
    )
    db_session.add(current_root)
    await db_session.flush()
    series = Series(
        title="Default Hydration",
        sort_title="default hydration",
        comicvine_id=4020,
        library_root_id=current_root.id,
    )
    db_session.add(series)
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )

    plan = await load_catalog_hydration_plan(
        session_factory,
        series_id=series.id,
        search_on_add=False,
    )

    assert plan is not None
    assert plan.library_root_id is None


async def test_run_pending_catalog_hydration_reuses_step_2_persistent_cache(
    db_session,
    tmp_path,
) -> None:
    series = Series(
        title="Partial Cached Hydration",
        sort_title="partial cached hydration",
        year_start=2026,
        comicvine_id=9101,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add(series)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    step_2_provider = CatalogHydrationProviderDouble()
    step_2_cache = PersistentComicVineCacheProvider(step_2_provider, session_factory)
    await step_2_cache.get_series("9101")
    await step_2_cache.get_issues_for_series("9101")

    assert step_2_provider.series_calls == 1
    assert step_2_provider.issue_list_calls == 1

    hydration_provider = CatalogHydrationProviderDouble(fail_on_fetch=True)
    hydration_cache = PersistentComicVineCacheProvider(hydration_provider, session_factory)
    metadata_service = MetadataService(hydration_cache, covers_dir=tmp_path)
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    series_service = SeriesService(metadata_service, event_bus)

    recovered = await run_pending_catalog_hydration(
        session_factory,
        series_service=series_service,
    )

    assert recovered == 1
    assert hydration_provider.series_calls == 0
    assert hydration_provider.issue_list_calls == 0
    await db_session.refresh(series)
    assert series.issue_catalog_state == IssueCatalogState.COMPLETE
    assert series.title == "Cached Hydration"
    event_bus.emit.assert_awaited_once()


async def test_run_pending_catalog_hydration_continues_after_series_failure(db_session) -> None:
    failed = Series(
        title="Failed Restart Recovery",
        sort_title="failed restart recovery",
        year_start=2026,
        comicvine_id=3001,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    recovered_series = Series(
        title="Recovered After Failure",
        sort_title="recovered after failure",
        year_start=2026,
        comicvine_id=3002,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add_all([failed, recovered_series])
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )
    service = CatalogHydrationSeriesServiceStub(failing_cv_ids={3001})

    recovered = await run_pending_catalog_hydration(
        session_factory,
        series_service=service,
    )

    assert recovered == 1
    assert service.prefetch_calls == [3001, 3002]
    assert service.add_calls == [(3002, False)]
    await db_session.refresh(failed)
    await db_session.refresh(recovered_series)
    assert failed.issue_catalog_state == IssueCatalogState.FAILED
    assert failed.issue_catalog_error == "ComicVine failed for 3001"
    assert recovered_series.issue_catalog_state == IssueCatalogState.COMPLETE


async def test_mark_catalog_hydration_failed_sets_retryable_state(db_session) -> None:
    series = Series(
        title="Hydration Failure",
        sort_title="hydration failure",
        year_start=2026,
        comicvine_id=123456,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add(series)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=type(db_session),
        expire_on_commit=False,
    )

    await mark_catalog_hydration_failed(
        session_factory,
        series_id=series.id,
        error="ComicVine timed out",
    )

    await db_session.refresh(series)
    assert series.issue_catalog_state == IssueCatalogState.FAILED
    assert series.issue_catalog_error == "ComicVine timed out"
    assert series.issue_catalog_last_synced_at is None
    assert series.issue_catalog_last_checked_at is None


async def test_restart_catalog_hydration_publishes_one_import_summary(db_session) -> None:
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    series = Series(
        title="Imported",
        sort_title="imported",
        comicvine_id=4001,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add_all([job, series])
    await db_session.flush()
    db_session.add(
        ImportedSeries(
            import_job_id=job.id,
            series_id=series.id,
            raw_series_name="Imported",
            status=ImportSeriesStatus.IMPORTED,
        )
    )
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    await run_pending_catalog_hydration(factory, series_service=CatalogHydrationSeriesServiceStub())

    progress = (await db_session.scalars(sa_select(OperationProgress))).one_or_none()
    assert progress is not None
    assert progress.operation_key == f"metadata:{job.id}"
    assert progress.state == OperationProgressState.COMPLETED
    assert progress.overall_percent == 100
    assert progress.item_key is None
