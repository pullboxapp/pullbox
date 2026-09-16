"""Progress helper functions for Step 2 file matching."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.models.import_job import ImportJobStatus
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_file_match_targets import (
    load_file_match_target_index as _load_file_match_target_index,
)
from pullbox.services.import_progress_runtime import (
    ScanReviewProgressPlan,
    current_item_payload,
    scan_review_completed_weight,
    scan_review_progress_pct,
)

_FILE_MATCH_PROGRESS_HEARTBEAT_SECONDS = 5.0

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob
    from pullbox.providers.base import MetadataProvider
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex

    ProgressCallback = Callable[[ImportProgressEvent], Awaitable[None]]
    EmitProgressFunc = Callable[
        [AsyncSession, ImportJob, ImportProgressEvent, ProgressCallback],
        Awaitable[None],
    ]
    EmitLiveProgressFunc = Callable[..., Awaitable[None]]
    PhaseProgressFunc = Callable[[int, int, int, int], int]
    EstimateRemainingFunc = Callable[[datetime | None, int], int | None]

    class EstimateRemainingWorkFunc(Protocol):
        def __call__(
            self,
            started_at: datetime | None,
            *,
            completed_units: int | float,
            total_units: int | float,
            current_unit_progress_pct: int | float | None = None,
        ) -> int | None: ...

    JobStatsFunc = Callable[[ImportJob], dict[str, int]]
    EmitFileMatchingProgressFunc = Callable[..., Awaitable[None]]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    LoadFileMatchTargetIndexFunc = Callable[..., Awaitable[FileMatchTargetIndex]]


def _file_match_item_heartbeat_progress(elapsed_seconds: int) -> int:
    """Return item-local progress while issue targets are loading."""
    heartbeat_count = max((max(elapsed_seconds, 1) + 4) // 5, 1)
    return min(45, 5 + heartbeat_count * 10)


def build_file_matching_progress_emitter(
    *,
    session: AsyncSession,
    job: ImportJob,
    progress_callback: ProgressCallback | None,
    emit_progress: EmitProgressFunc,
    emit_live_progress: EmitLiveProgressFunc,
    phase_progress: PhaseProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    job_stats: JobStatsFunc,
    total_file_phase_units: int,
    revision_state: dict[str, int],
    estimate_remaining_work_seconds: EstimateRemainingWorkFunc | None = None,
    scan_review_plan: ScanReviewProgressPlan | None = None,
    phase_start: int = 80,
    phase_end: int = 99,
    work_started_at: datetime | None = None,
) -> Callable[..., Awaitable[None]]:
    """Build the Step 2 file-matching progress emitter."""

    async def emit_file_matching_progress(
        item: ImportedSeries,
        completed_units: int,
        *,
        message: str,
        current_item_stage: str = "file_matching",
        current_item_progress_pct: int | None = None,
        current_work_unit_progress_pct: int | float | None = None,
        live_only: bool = False,
    ) -> None:
        if progress_callback is None:
            return

        work_unit_progress_pct = (
            current_work_unit_progress_pct
            if current_work_unit_progress_pct is not None
            else current_item_progress_pct
        )
        completed_weight = (
            scan_review_completed_weight(
                scan_review_plan,
                phase="file_matching",
                completed_items=completed_units,
                current_item_progress_pct=work_unit_progress_pct,
            )
            if scan_review_plan is not None
            else None
        )
        progress = (
            scan_review_progress_pct(scan_review_plan, completed_weight=completed_weight)
            if scan_review_plan is not None and completed_weight is not None
            else phase_progress(
                phase_start,
                phase_end,
                completed_units,
                max(total_file_phase_units, 1),
            )
        )
        estimated_seconds_remaining = (
            estimate_remaining_work_seconds(
                work_started_at
                or job.scan_completed_at
                or job.match_completed_at
                or job.scan_started_at,
                completed_units=(
                    completed_weight if completed_weight is not None else completed_units
                ),
                total_units=(
                    scan_review_plan.total_weight
                    if scan_review_plan is not None
                    else max(total_file_phase_units, 1)
                ),
                current_unit_progress_pct=(
                    None if scan_review_plan is not None else work_unit_progress_pct
                ),
            )
            if estimate_remaining_work_seconds is not None
            else estimate_remaining_seconds(
                job.scan_started_at,
                progress,
            )
        )
        event = ImportProgressEvent(
            job_id=job.id,
            status=ImportJobStatus.FILE_MATCHING,
            phase="file_matching",
            progress=progress,
            message=message,
            current_series=item.raw_series_name,
            current_series_status=item.status,
            estimated_seconds_remaining=estimated_seconds_remaining,
            **current_item_payload(
                kind="series",
                stage=current_item_stage,
                name=item.raw_series_name,
                progress_pct=(
                    current_item_progress_pct if current_item_progress_pct is not None else 0
                ),
            ),
            **job_stats(job),
        )
        if live_only:
            await emit_live_progress(
                job,
                event,
                progress_callback=progress_callback,
                revision_state=revision_state,
                started_at=job.scan_started_at,
            )
            return
        await emit_progress(session, job, event, progress_callback)
        revision_state["value"] = max(
            revision_state["value"],
            int(event.progress_revision or 0),
        )

    return emit_file_matching_progress


async def load_file_match_target_index_with_progress(
    *,
    session: AsyncSession,
    job_id: int,
    item: ImportedSeries,
    files_to_match: list[ImportedFile],
    series_file_count: int | None = None,
    duplicate_series: bool,
    metadata_provider: MetadataProvider | None,
    series_idx: int,
    total_series: int,
    completed_units: int,
    progress_callback: ProgressCallback | None,
    emit_file_matching_progress: EmitFileMatchingProgressFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    load_file_match_target_index: LoadFileMatchTargetIndexFunc = _load_file_match_target_index,
    heartbeat_seconds: float | None = None,
) -> FileMatchTargetIndex:
    """Load issue targets with visible Step 2 heartbeat progress."""
    if progress_callback is None:
        return await load_file_match_target_index(
            session,
            item,
            duplicate_series=duplicate_series,
            metadata_provider=metadata_provider,
            files=files_to_match,
            series_file_count=series_file_count,
        )

    await emit_file_matching_progress(
        item,
        completed_units,
        message=(
            f"Loading issue targets for {item.raw_series_name} "
            f"(series {series_idx + 1}/{total_series})..."
        ),
        current_item_stage="file_matching",
        current_item_progress_pct=5,
        current_work_unit_progress_pct=0,
    )

    if item.series_id is not None or metadata_provider is None:
        return await load_file_match_target_index(
            session,
            item,
            duplicate_series=duplicate_series,
            metadata_provider=metadata_provider,
            files=files_to_match,
            series_file_count=series_file_count,
        )

    heartbeat_interval = (
        heartbeat_seconds
        if heartbeat_seconds is not None
        else _FILE_MATCH_PROGRESS_HEARTBEAT_SECONDS
    )

    async def _load_targets() -> FileMatchTargetIndex:
        return await load_file_match_target_index(
            session,
            item,
            duplicate_series=duplicate_series,
            metadata_provider=metadata_provider,
            files=files_to_match,
            series_file_count=series_file_count,
        )

    task: asyncio.Task[FileMatchTargetIndex] = asyncio.create_task(_load_targets())
    started_at = asyncio.get_running_loop().time()
    try:
        while not task.done():
            done, _pending = await asyncio.wait(
                {task},
                timeout=heartbeat_interval,
            )
            if done:
                break
            await raise_if_cancelled(session, job_id)
            elapsed_seconds = max(
                round(asyncio.get_running_loop().time() - started_at),
                1,
            )
            await emit_file_matching_progress(
                item,
                completed_units,
                message=(
                    f"Still loading issue targets for {item.raw_series_name} "
                    f"({elapsed_seconds}s elapsed)... "
                    "Large series can take a few minutes."
                ),
                current_item_stage="file_matching",
                current_item_progress_pct=_file_match_item_heartbeat_progress(elapsed_seconds),
                current_work_unit_progress_pct=_file_match_item_heartbeat_progress(elapsed_seconds),
                live_only=True,
            )
        return await task
    except (JobPausedError, JobCancelledError):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
