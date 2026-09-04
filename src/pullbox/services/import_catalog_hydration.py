"""Background catalog hydration helpers for targeted-first imports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select as sa_select

from pullbox.core.library_root_resolution import preferred_managed_root_id
from pullbox.models.series import IssueCatalogState, Series
from pullbox.services.import_metadata_priority import catalog_metadata_work
from pullbox.services.import_metadata_progress import (
    catalog_hydration_import_job_ids,
    track_import_metadata_progress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

catalog_hydration_tasks: set[asyncio.Task[None]] = set()
_catalog_hydration_semaphore: asyncio.Semaphore | None = None
_catalog_hydration_semaphore_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True, slots=True)
class CatalogHydrationPlan:
    comicvine_id: int
    library_root_id: int | None
    search_on_add: bool


@dataclass(frozen=True, slots=True)
class PendingCatalogHydration:
    series_id: int
    search_on_add: bool


def reset_catalog_hydration_gate() -> None:
    """Reset the app-local hydration gate for tests and loop restarts."""
    global _catalog_hydration_semaphore, _catalog_hydration_semaphore_loop
    _catalog_hydration_semaphore = None
    _catalog_hydration_semaphore_loop = None


def schedule_catalog_hydration(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    series_service: Any,
    series_id: int,
    search_on_add: bool,
) -> None:
    """Nudge the singleton catalog drain after the Step 4 file-placement hot path."""
    if session_factory is None:
        return
    if _catalog_hydration_methods(series_service) is None:
        return
    if any(not task.done() for task in catalog_hydration_tasks):
        return

    async def run_hydration() -> None:
        _ = series_id, search_on_add
        while True:
            try:
                await run_pending_catalog_hydration(
                    session_factory,
                    series_service=series_service,
                )
            except Exception as exc:
                logger.warning(
                    "import_catalog_hydration_drain_failed",
                    error=str(exc),
                )
                return
            await asyncio.sleep(0)
            if not await load_pending_catalog_hydration(session_factory, limit=1):
                return

    task = asyncio.create_task(run_hydration())
    catalog_hydration_tasks.add(task)
    task.add_done_callback(catalog_hydration_tasks.discard)


async def run_pending_catalog_hydration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    series_service: Any,
    limit: int | None = None,
) -> int:
    """Resume full catalog hydration rows abandoned by restart or task loss."""
    hydration_methods = _catalog_hydration_methods(series_service)
    if hydration_methods is None:
        return 0
    prefetch_comicvine_bundle, add_from_comicvine_prefetched = hydration_methods
    pending = await load_pending_catalog_hydration(session_factory, limit=limit)
    recovered = 0

    job_ids = await catalog_hydration_import_job_ids(session_factory)
    async with (
        catalog_metadata_work(len(pending)) as priority,
        track_import_metadata_progress(session_factory, job_ids=job_ids),
        catalog_hydration_gate(),
    ):
        batch_methods = _catalog_hydration_batch_methods(series_service)
        if batch_methods is not None and pending:
            return await _run_pending_catalog_hydration_batch(
                session_factory,
                pending=pending,
                batch_methods=batch_methods,
                add_from_comicvine_prefetched=add_from_comicvine_prefetched,
                priority=priority,
            )
        for request in pending:
            try:
                hydrated = await run_catalog_hydration(
                    session_factory,
                    series_id=request.series_id,
                    search_on_add=request.search_on_add,
                    prefetch_comicvine_bundle=prefetch_comicvine_bundle,
                    add_from_comicvine_prefetched=add_from_comicvine_prefetched,
                )
            except Exception as exc:
                await mark_catalog_hydration_failed(
                    session_factory,
                    series_id=request.series_id,
                    error=str(exc),
                )
                logger.warning(
                    "import_catalog_hydration_recovery_failed",
                    series_id=request.series_id,
                    error=str(exc),
                )
                continue
            finally:
                await priority.complete_one()

            if not hydrated:
                continue

            recovered += 1
            logger.info(
                "import_catalog_hydration_recovered",
                series_id=request.series_id,
            )

    return recovered


def _catalog_hydration_batch_methods(
    series_service: Any,
) -> (
    tuple[
        Callable[..., Awaitable[Any]],
        Callable[..., Awaitable[Any]],
        Callable[..., Awaitable[Any]],
    ]
    | None
):
    methods = (
        getattr(series_service, "prefetch_comicvine_profiles", None),
        getattr(series_service, "prefetch_comicvine_issue_catalogs", None),
        getattr(series_service, "upsert_comicvine_profile", None),
    )
    if not all(callable(method) for method in methods):
        return None
    return methods  # type: ignore[return-value]


async def _run_pending_catalog_hydration_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pending: list[PendingCatalogHydration],
    batch_methods: tuple[
        Callable[..., Awaitable[Any]],
        Callable[..., Awaitable[Any]],
        Callable[..., Awaitable[Any]],
    ],
    add_from_comicvine_prefetched: Callable[..., Awaitable[Series]],
    priority: Any,
) -> int:
    """Persist visible profiles first, then complete issue catalogs in bulk."""
    prefetch_profiles, prefetch_catalogs, upsert_profile = batch_methods
    planned: list[tuple[PendingCatalogHydration, CatalogHydrationPlan]] = []
    for request in pending:
        plan = await load_catalog_hydration_plan(
            session_factory,
            series_id=request.series_id,
            search_on_add=request.search_on_add,
            only_if_hydrating=True,
        )
        if plan is None:
            await priority.complete_one()
            continue
        planned.append((request, plan))

    comicvine_ids = [plan.comicvine_id for _request, plan in planned]
    if not comicvine_ids:
        return 0

    try:
        profiles = await prefetch_profiles(comicvine_ids)
    except Exception as exc:
        for request, _plan in planned:
            await mark_catalog_hydration_failed(
                session_factory,
                series_id=request.series_id,
                error=str(exc),
            )
            await priority.complete_one()
        return 0

    eligible: list[tuple[PendingCatalogHydration, CatalogHydrationPlan]] = []
    for request, plan in planned:
        profile = profiles.get(plan.comicvine_id)
        if profile is None:
            await mark_catalog_hydration_failed(
                session_factory,
                series_id=request.series_id,
                error="ComicVine did not return the requested series profile",
            )
            await priority.complete_one()
            continue
        try:
            async with session_factory() as session:
                state = await session.scalar(
                    sa_select(Series.issue_catalog_state).where(Series.id == request.series_id)
                )
                if state != IssueCatalogState.HYDRATING:
                    await priority.complete_one()
                    continue
                await upsert_profile(session, plan.comicvine_id, profile)
                await session.commit()
            eligible.append((request, plan))
        except Exception as exc:
            await mark_catalog_hydration_failed(
                session_factory,
                series_id=request.series_id,
                error=str(exc),
            )
            await priority.complete_one()

    if not eligible:
        return 0

    try:
        catalogs = await prefetch_catalogs([plan.comicvine_id for _request, plan in eligible])
    except Exception as exc:
        for request, _plan in eligible:
            await mark_catalog_hydration_failed(
                session_factory,
                series_id=request.series_id,
                error=str(exc),
            )
            await priority.complete_one()
        return 0

    recovered = 0
    for request, plan in eligible:
        try:
            profile = profiles[plan.comicvine_id]
            summaries = catalogs.get(plan.comicvine_id)
            if summaries is None:
                raise ValueError("ComicVine did not return the requested issue catalog")
            persisted = await _persist_catalog_hydration(
                session_factory,
                series_id=request.series_id,
                plan=plan,
                series_meta=profile,
                issue_summaries=summaries,
                add_from_comicvine_prefetched=add_from_comicvine_prefetched,
            )
            if persisted:
                recovered += 1
                logger.info("import_catalog_hydration_recovered", series_id=request.series_id)
        except Exception as exc:
            await mark_catalog_hydration_failed(
                session_factory,
                series_id=request.series_id,
                error=str(exc),
            )
        finally:
            await priority.complete_one()
    return recovered


async def load_pending_catalog_hydration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int | None = None,
) -> list[PendingCatalogHydration]:
    """Load incomplete catalog rows that should resume after process restart."""
    async with session_factory() as session:
        stmt = (
            sa_select(Series.id, Series.monitored)
            .where(
                Series.issue_catalog_state == IssueCatalogState.HYDRATING,
                Series.comicvine_id.isnot(None),
            )
            .order_by(Series.id.asc())
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return [
            PendingCatalogHydration(
                series_id=int(series_id),
                search_on_add=bool(monitored),
            )
            for series_id, monitored in result.all()
        ]


def catalog_hydration_gate() -> asyncio.Semaphore:
    """Return the app-local lane for background catalog hydration."""
    global _catalog_hydration_semaphore, _catalog_hydration_semaphore_loop

    loop = asyncio.get_running_loop()
    if _catalog_hydration_semaphore is None or _catalog_hydration_semaphore_loop is not loop:
        _catalog_hydration_semaphore = asyncio.Semaphore(1)
        _catalog_hydration_semaphore_loop = loop
    return _catalog_hydration_semaphore


def _catalog_hydration_methods(
    series_service: Any,
) -> (
    tuple[
        Callable[[int], Awaitable[tuple[Any, list[Any]]]],
        Callable[..., Awaitable[Series]],
    ]
    | None
):
    prefetch_comicvine_bundle = getattr(series_service, "prefetch_comicvine_bundle", None)
    add_from_comicvine_prefetched = getattr(series_service, "add_from_comicvine_prefetched", None)
    if not callable(prefetch_comicvine_bundle) or not callable(add_from_comicvine_prefetched):
        return None
    return prefetch_comicvine_bundle, add_from_comicvine_prefetched


async def run_catalog_hydration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    series_id: int,
    search_on_add: bool,
    prefetch_comicvine_bundle: Callable[[int], Awaitable[tuple[Any, list[Any]]]],
    add_from_comicvine_prefetched: Callable[..., Awaitable[Series]],
) -> bool:
    plan = await load_catalog_hydration_plan(
        session_factory,
        series_id=series_id,
        search_on_add=search_on_add,
        only_if_hydrating=True,
    )
    if plan is None:
        return False

    # ComicVine can be slow for giant series. Fetch outside any DB session so
    # the UI and active import writer keep access to the pool while we wait.
    series_meta, issue_summaries = await prefetch_comicvine_bundle(plan.comicvine_id)

    return await _persist_catalog_hydration(
        session_factory,
        series_id=series_id,
        plan=plan,
        series_meta=series_meta,
        issue_summaries=issue_summaries,
        add_from_comicvine_prefetched=add_from_comicvine_prefetched,
    )


async def _persist_catalog_hydration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    series_id: int,
    plan: CatalogHydrationPlan,
    series_meta: Any,
    issue_summaries: list[Any],
    add_from_comicvine_prefetched: Callable[..., Awaitable[Series]],
) -> bool:
    """Persist one prefetched catalog only while its exact row still needs it."""

    async with session_factory() as hydrate_session:
        # A cancellation rollback can delete the series while the provider
        # request is in flight. Revalidate the exact row under the write
        # transaction so the general ComicVine upsert path cannot recreate a
        # series that the rollback already removed.
        persisted_series_id = await hydrate_session.scalar(
            sa_select(Series.id)
            .where(
                Series.id == series_id,
                Series.comicvine_id == plan.comicvine_id,
                Series.issue_catalog_state == IssueCatalogState.HYDRATING,
            )
            .with_for_update()
        )
        if persisted_series_id is None:
            return False
        await add_from_comicvine_prefetched(
            hydrate_session,
            comicvine_id=plan.comicvine_id,
            library_root_id=plan.library_root_id,
            search_on_add=plan.search_on_add,
            series_meta=series_meta,
            issue_summaries=issue_summaries,
        )
        await hydrate_session.commit()
    return True


async def load_catalog_hydration_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    series_id: int,
    search_on_add: bool,
    only_if_hydrating: bool = False,
) -> CatalogHydrationPlan | None:
    async with session_factory() as session:
        series = await session.get(Series, series_id)
        if series is None:
            return None
        if only_if_hydrating and series.issue_catalog_state != IssueCatalogState.HYDRATING:
            return None
        if series.comicvine_id is None:
            msg = "Series has no ComicVine ID"
            raise ValueError(msg)

        series.issue_catalog_state = IssueCatalogState.HYDRATING
        series.issue_catalog_error = None
        series.issue_catalog_last_synced_at = None
        series.issue_catalog_last_checked_at = None
        await session.commit()

        return CatalogHydrationPlan(
            comicvine_id=int(series.comicvine_id),
            library_root_id=preferred_managed_root_id(series),
            search_on_add=search_on_add,
        )


async def mark_catalog_hydration_failed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    series_id: int,
    error: str,
) -> None:
    async with session_factory() as session:
        series = await session.get(Series, series_id)
        if series is None:
            return
        series.issue_catalog_state = IssueCatalogState.FAILED
        series.issue_catalog_error = error
        series.issue_catalog_last_synced_at = None
        series.issue_catalog_last_checked_at = None
        await session.commit()
