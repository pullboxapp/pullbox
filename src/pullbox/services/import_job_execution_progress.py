"""Progress helper functions for Step 4 import execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import and_, or_
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.core.sqlite_lock import is_sqlite_locked_error
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_active_file_progress import (
    ActiveFileProgressSettings,
    active_file_progress_pct,
)
from pullbox.services.import_progress_runtime import (
    ImportGroupProgressPlan,
    ImportProgressFileProfile,
    ImportProgressSettings,
    current_item_payload,
    import_group_file_progress_pct,
    import_group_metadata_progress_pct,
    import_group_progress_plan,
    weighted_import_progress_pct,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.import_job_execution_types import (
        EstimateRemainingFunc,
        ExecutionItemPlan,
        RaiseIfCancelledFunc,
        ReportFileProgressFunc,
    )

logger = structlog.get_logger(__name__)

_IMPORT_PROGRESS_PLAN_BATCH_SIZE = 500
_IMPORTABLE_FILE_STATUSES = (
    ImportedFileStatus.MATCHED,
    ImportedFileStatus.CONFIRMED,
)


async def reconcile_durable_import_execution_counters(
    session: AsyncSession,
    job: ImportJob,
) -> None:
    """Rebuild execution totals from file and series rows committed before interruption."""
    file_status_result = await session.execute(
        sa_select(ImportedFile.status, sa_func.count(ImportedFile.id))
        .where(ImportedFile.import_job_id == job.id)
        .group_by(ImportedFile.status)
    )
    file_status_counts = {status: count for status, count in file_status_result.all()}
    series_status_result = await session.execute(
        sa_select(ImportedSeries.status, sa_func.count(ImportedSeries.id))
        .where(ImportedSeries.import_job_id == job.id)
        .group_by(ImportedSeries.status)
    )
    series_status_counts = {status: count for status, count in series_status_result.all()}

    job.total_files_imported = file_status_counts.get(ImportedFileStatus.IMPORTED, 0)
    job.total_files_failed = file_status_counts.get(ImportedFileStatus.FAILED, 0)
    job.series_imported = series_status_counts.get(ImportSeriesStatus.IMPORTED, 0)
    job.series_failed = series_status_counts.get(ImportSeriesStatus.FAILED, 0)


async def build_import_group_progress_plans(
    session: AsyncSession,
    execution_items: list[ExecutionItemPlan],
    settings: ImportProgressSettings,
) -> dict[int, ImportGroupProgressPlan]:
    """Build weighted progress plans for selected Step 4 review groups."""
    modes_by_item_id = {item.item_id: item.mode for item in execution_items}
    profiles_by_item_id: dict[int, list[ImportProgressFileProfile]] = {
        item.item_id: [] for item in execution_items
    }

    for start in range(0, len(execution_items), _IMPORT_PROGRESS_PLAN_BATCH_SIZE):
        batch = execution_items[start : start + _IMPORT_PROGRESS_PLAN_BATCH_SIZE]
        batch_ids = [item.item_id for item in batch]
        files_result = await session.execute(
            sa_select(ImportedFile)
            .select_from(ImportedSeries)
            .join(
                ImportedFile,
                and_(
                    ImportedFile.import_job_id == ImportedSeries.import_job_id,
                    ImportedFile.import_series_id == ImportedSeries.id,
                ),
            )
            .where(
                ImportedSeries.id.in_(batch_ids),
                or_(
                    ImportedFile.status.in_(_IMPORTABLE_FILE_STATUSES),
                    and_(
                        ImportedFile.status == ImportedFileStatus.CONFLICT,
                        ImportedFile.is_preferred.is_(True),
                    ),
                ),
            )
            .order_by(ImportedFile.import_series_id.asc(), ImportedFile.id.asc())
        )
        for imp_file in files_result.scalars().all():
            item_id = int(imp_file.import_series_id)
            mode = modes_by_item_id.get(item_id)
            if mode is None:
                continue
            if mode == "duplicate" and (
                imp_file.status not in _IMPORTABLE_FILE_STATUSES or not imp_file.include_in_import
            ):
                continue
            profiles_by_item_id[item_id].append(
                ImportProgressFileProfile(
                    file_id=imp_file.id,
                    file_path=imp_file.file_path,
                    file_size=imp_file.file_size,
                )
            )

    return {
        item.item_id: import_group_progress_plan(
            settings,
            profiles_by_item_id[item.item_id],
        )
        for item in execution_items
    }


def progress_session_factory_for_runtime(
    session: AsyncSession,
) -> async_sessionmaker[AsyncSession] | None:
    """Create an isolated progress session factory when the DB URL supports it."""
    bind = session.bind
    if bind is None:
        return None
    bind_url = str(bind.sync_engine.url)
    if ":memory:" in bind_url:
        return None
    return async_sessionmaker(
        bind,
        class_=type(session),
        expire_on_commit=False,
    )


async def emit_active_file_progress(
    session: AsyncSession,
    progress_session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    job_id: int,
    event: ImportProgressEvent,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
) -> None:
    """Persist active-file progress without letting SQLite lock contention fail imports."""
    from pullbox.services.import_workflow_state import emit_progress as persist_progress_event

    try:
        if progress_session_factory is None:
            job = await session.get(ImportJob, job_id)
            if job is None:
                return
            await persist_progress_event(session, job, event, progress_callback)
            return

        async with progress_session_factory() as progress_session:
            progress_job = await progress_session.get(ImportJob, job_id)
            if progress_job is None:
                return
            await persist_progress_event(progress_session, progress_job, event, progress_callback)
    except Exception as exc:
        if not is_sqlite_locked_error(exc):
            raise
        logger.warning(
            "import_active_file_progress_persist_skipped_sqlite_locked",
            job_id=job_id,
            current_file_name=event.current_file_name,
            current_file_stage=event.current_file_stage,
            error=str(exc),
        )
        if progress_callback is not None:
            await progress_callback(event)


def build_report_file_progress_callback(
    *,
    session: AsyncSession,
    job_id: int,
    job: ImportJob,
    job_started_at: datetime | None,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
    progress_session_factory: async_sessionmaker[AsyncSession] | None,
    estimate_remaining_seconds: EstimateRemainingFunc,
    group_progress_plans: dict[int, ImportGroupProgressPlan],
    shared_progress_settings: ImportProgressSettings,
    group_progress_weights: list[float],
    group_index: int,
    total_groups: int,
    series_id: int,
    series_name: str,
    series_found: int,
    stats: dict[str, int],
    progress_state: dict[str, object],
    revision_state: dict[str, int],
    active_file_progress_settings: ActiveFileProgressSettings,
    monotonic_time: Callable[[], float],
    active_file_progress_emit_interval_seconds: float,
    live_only_active_file_stages: frozenset[str],
    emit_active_file_progress: Callable[..., Awaitable[None]],
    emit_live_progress: Callable[..., Awaitable[None]],
) -> ReportFileProgressFunc:
    """Build the per-review-group active-file progress callback."""

    async def report_file_progress(
        *,
        imp_file: ImportedFile,
        file_index: int,
        total_files: int,
        stage: str,
        current: int,
        total: int,
        unit: str,
        live_only: bool = False,
    ) -> None:
        current_file_pct = active_file_progress_pct(
            active_file_progress_settings,
            imp_file,
            stage,
            current,
            total,
        )
        group_plan = group_progress_plans.get(
            series_id,
            import_group_progress_plan(shared_progress_settings, []),
        )
        group_progress_pct = import_group_file_progress_pct(
            group_plan,
            file_index=file_index,
            current_file_pct=current_file_pct,
        )
        overall_progress = weighted_import_progress_pct(
            group_progress_weights,
            current_group_index=group_index,
            current_group_progress_pct=group_progress_pct,
        )
        loop_now = monotonic_time()
        emitted_at_value = progress_state.get("emitted_at")
        emitted_at = float(emitted_at_value) if isinstance(emitted_at_value, int | float) else 0.0
        should_emit = (
            progress_state["file_id"] != imp_file.id
            or progress_state["stage"] != stage
            or progress_state["pct"] != current_file_pct
            or current >= max(total, 1)
            or loop_now - emitted_at >= active_file_progress_emit_interval_seconds
        )
        if not should_emit:
            return

        stage_changed = progress_state["file_id"] != imp_file.id or progress_state["stage"] != stage
        stage_complete = current >= max(total, 1)
        persist_progress = (
            not live_only
            and stage not in live_only_active_file_stages
            and (stage_changed or stage_complete)
        )

        progress_state.update(
            {
                "file_id": imp_file.id,
                "stage": stage,
                "pct": current_file_pct,
                "emitted_at": loop_now,
            }
        )

        event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            ephemeral_progress=not persist_progress,
            mode="import",
            phase="importing",
            progress=overall_progress,
            message=(
                f"Processing file {file_index}/{max(total_files, 1)} "
                f"in review group {group_index + 1}/{max(total_groups, 1)}"
            ),
            current_series_id=series_id,
            current_series_name=series_name,
            current_file_id=imp_file.id,
            current_file_name=imp_file.file_name,
            current_file_stage=stage,
            current_file_progress_current=current,
            current_file_progress_total=total,
            current_file_progress_pct=current_file_pct,
            current_file_progress_unit=unit,
            current_series=series_name,
            current_series_status=ImportSeriesStatus.IMPORTING,
            estimated_seconds_remaining=estimate_remaining_seconds(
                job_started_at,
                overall_progress,
            ),
            series_imported=int(stats["series_imported"]),
            series_failed=int(stats["series_failed"]),
            series_found=series_found,
            total_files_imported=int(stats["total_files_imported"]),
            total_files_failed=int(stats["total_files_failed"]),
            **current_item_payload(
                kind="file",
                stage=stage,
                name=imp_file.file_name,
                progress_pct=current_file_pct,
            ),
        )
        if not persist_progress:
            await emit_live_progress(
                job,
                event,
                progress_callback=progress_callback,
                revision_state=revision_state,
                started_at=job_started_at,
            )
            return

        revision_state["value"] = max(
            revision_state["value"] + 1,
            int(event.progress_revision or 0),
        )
        event.progress_revision = revision_state["value"]
        await emit_active_file_progress(
            session,
            progress_session_factory,
            job_id=job_id,
            progress_callback=progress_callback,
            event=event,
        )

    return report_file_progress


def build_series_metadata_progress_emitter(
    *,
    session: AsyncSession,
    job_id: int,
    job: ImportJob,
    job_started_at: datetime | None,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
    emit_progress: Callable[..., Awaitable[None]],
    emit_live_progress: Callable[..., Awaitable[None]],
    estimate_remaining_seconds: EstimateRemainingFunc,
    group_progress_plans: dict[int, ImportGroupProgressPlan],
    shared_progress_settings: ImportProgressSettings,
    group_progress_weights: list[float],
    stats: Callable[[], dict[str, int]],
    series_found: int,
    revision_state: dict[str, int],
) -> Callable[..., Awaitable[None]]:
    """Build the per-review-group metadata progress emitter."""

    async def emit_series_metadata_progress(
        *,
        group_index: int,
        total_groups: int,
        series_id: int,
        series_name: str,
        message: str,
        current_item_stage: str,
        current_item_progress_pct: int,
        live_only: bool = False,
    ) -> None:
        if progress_callback is None:
            return

        group_plan = group_progress_plans.get(
            series_id,
            import_group_progress_plan(shared_progress_settings, []),
        )
        group_progress = import_group_metadata_progress_pct(
            group_plan,
            metadata_progress_pct=current_item_progress_pct,
        )
        progress = weighted_import_progress_pct(
            group_progress_weights,
            current_group_index=group_index,
            current_group_progress_pct=group_progress,
        )
        current_stats = stats()
        event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            mode="import",
            phase="importing",
            progress=progress,
            message=message,
            current_series_id=series_id,
            current_series_name=series_name,
            current_series=series_name,
            current_series_status=ImportSeriesStatus.IMPORTING,
            estimated_seconds_remaining=estimate_remaining_seconds(
                job_started_at,
                progress,
            ),
            series_imported=current_stats["series_imported"],
            series_failed=current_stats["series_failed"],
            series_found=series_found,
            total_files_imported=current_stats["total_files_imported"],
            total_files_failed=current_stats["total_files_failed"],
            current_file_id=None,
            current_file_name=None,
            current_file_stage=None,
            current_file_progress_current=None,
            current_file_progress_total=None,
            current_file_progress_pct=None,
            current_file_progress_unit=None,
            **current_item_payload(
                kind="series",
                stage=current_item_stage,
                name=series_name,
                progress_pct=current_item_progress_pct,
            ),
        )
        if live_only:
            await emit_live_progress(
                job,
                event,
                progress_callback=progress_callback,
                revision_state=revision_state,
                started_at=job_started_at,
            )
            return

        revision_state["value"] += 1
        job.progress_revision = revision_state["value"]
        event.progress_revision = revision_state["value"]
        await emit_progress(session, job, event, progress_callback)

    return emit_series_metadata_progress


async def await_prefetch_with_metadata_progress(
    task: asyncio.Task[tuple[Any, list[Any]]],
    *,
    group_index: int,
    total_groups: int,
    series_id: int,
    series_name: str,
    session: AsyncSession,
    job_id: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
    emit_series_metadata_progress: Callable[..., Awaitable[None]],
    heartbeat_seconds: float,
    monotonic_time: Callable[[], float],
) -> tuple[Any, list[Any]]:
    """Await a ComicVine prefetch task while emitting metadata progress heartbeats."""
    await emit_series_metadata_progress(
        group_index=group_index,
        total_groups=total_groups,
        series_id=series_id,
        series_name=series_name,
        message=(
            f"Fetching ComicVine metadata for {series_name} "
            f"(review group {group_index + 1}/{max(total_groups, 1)})..."
        ),
        current_item_stage="metadata_fetch",
        current_item_progress_pct=8,
    )
    started_at = monotonic_time()
    try:
        while not task.done():
            done, _pending = await asyncio.wait(
                {task},
                timeout=heartbeat_seconds,
            )
            if done:
                break
            await raise_if_cancelled(session, job_id)
            elapsed_seconds = max(round(monotonic_time() - started_at), 1)
            await emit_series_metadata_progress(
                group_index=group_index,
                total_groups=total_groups,
                series_id=series_id,
                series_name=series_name,
                message=(
                    f"Still fetching ComicVine metadata for {series_name} "
                    f"({elapsed_seconds}s elapsed)... Large series can take a few minutes."
                ),
                current_item_stage="metadata_fetch_wait",
                current_item_progress_pct=min(60, 24 + ((elapsed_seconds // 5) * 12)),
                live_only=True,
            )
        result = await task
    except (JobPausedError, JobCancelledError):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    await emit_series_metadata_progress(
        group_index=group_index,
        total_groups=total_groups,
        series_id=series_id,
        series_name=series_name,
        message=f"Preparing series records for {series_name}...",
        current_item_stage="series_records",
        current_item_progress_pct=72,
    )
    return result
