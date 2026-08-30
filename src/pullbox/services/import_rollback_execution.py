"""Import rollback orchestration helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_comicinfo_enrichment import comicinfo_enrichment_gate
from pullbox.services.import_job_actions import (
    StoryArcManagedPlacementRollbackDeferredError,
)
from pullbox.services.import_workflow_state import sync_progress_snapshot_state

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

ProgressCallback = Callable[[ImportProgressEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RollbackActionPlan:
    """Primitive rollback plan entry safe across flushes and helper calls."""

    action_id: int
    sequence_no: int
    action_type: str
    payload: dict[str, object]


RollbackAction = Callable[["AsyncSession", RollbackActionPlan], Awaitable[None]]
RestoreReviewState = Callable[["AsyncSession", int], Awaitable[None]]
RecomputeFileCounters = Callable[["AsyncSession", ImportJob], Awaitable[None]]
RecomputeSeriesCounters = Callable[["AsyncSession", ImportJob], Awaitable[None]]
LogImportEvent = Callable[..., Awaitable[None]]
EmitProgress = Callable[
    ["AsyncSession", ImportJob, ImportProgressEvent, ProgressCallback],
    Awaitable[None],
]
EstimateRemainingSeconds = Callable[["datetime | None", int], int | None]
JobStats = Callable[[ImportJob], dict[str, int]]

ROLLBACK_ACTION_PAGE_SIZE = 500
ROLLBACK_PROGRESS_MIN_INTERVAL_SECONDS = 1.0


async def _count_completed_rollback_actions(session: AsyncSession, job_id: int) -> int:
    count = await session.scalar(
        sa_select(func.count(ImportJobAction.id)).where(
            ImportJobAction.import_job_id == job_id,
            ImportJobAction.status == ImportJobActionStatus.COMPLETED,
        )
    )
    return int(count or 0)


async def _iter_completed_rollback_actions(
    session: AsyncSession,
    job_id: int,
) -> AsyncIterator[RollbackActionPlan]:
    """Yield bounded reverse-order journal pages with a stable keyset cursor."""
    cursor: tuple[int, int] | None = None
    while True:
        statement = sa_select(ImportJobAction).where(
            ImportJobAction.import_job_id == job_id,
            ImportJobAction.status == ImportJobActionStatus.COMPLETED,
        )
        if cursor is not None:
            sequence_no, action_id = cursor
            statement = statement.where(
                or_(
                    ImportJobAction.sequence_no < sequence_no,
                    and_(
                        ImportJobAction.sequence_no == sequence_no,
                        ImportJobAction.id < action_id,
                    ),
                )
            )
        result = await session.execute(
            statement.order_by(
                ImportJobAction.sequence_no.desc(),
                ImportJobAction.id.desc(),
            ).limit(ROLLBACK_ACTION_PAGE_SIZE)
        )
        page = [
            RollbackActionPlan(
                action_id=action.id,
                sequence_no=action.sequence_no,
                action_type=action.action_type,
                payload=dict(action.payload or {}),
            )
            for action in result.scalars().all()
        ]
        if not page:
            return
        cursor = (page[-1].sequence_no, page[-1].action_id)
        for action in page:
            yield action
        if len(page) < ROLLBACK_ACTION_PAGE_SIZE:
            return


async def rollback_import_job(
    session: AsyncSession,
    job_id: int,
    *,
    rollback_action: RollbackAction,
    restore_review_state: RestoreReviewState,
    recompute_series_counters: RecomputeSeriesCounters,
    recompute_file_counters: RecomputeFileCounters,
    log_event: LogImportEvent,
    emit_progress: EmitProgress,
    estimate_remaining_seconds: EstimateRemainingSeconds,
    job_stats: JobStats,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Rollback durable import actions in reverse execution order."""
    async with comicinfo_enrichment_gate():
        return await _rollback_import_job_while_enrichment_fenced(
            session,
            job_id,
            rollback_action=rollback_action,
            restore_review_state=restore_review_state,
            recompute_series_counters=recompute_series_counters,
            recompute_file_counters=recompute_file_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            estimate_remaining_seconds=estimate_remaining_seconds,
            job_stats=job_stats,
            progress_callback=progress_callback,
        )


async def _rollback_import_job_while_enrichment_fenced(
    session: AsyncSession,
    job_id: int,
    *,
    rollback_action: RollbackAction,
    restore_review_state: RestoreReviewState,
    recompute_series_counters: RecomputeSeriesCounters,
    recompute_file_counters: RecomputeFileCounters,
    log_event: LogImportEvent,
    emit_progress: EmitProgress,
    estimate_remaining_seconds: EstimateRemainingSeconds,
    job_stats: JobStats,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Execute rollback while holding the process-local filesystem fence."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    total = await _count_completed_rollback_actions(session, job_id)
    await log_event(
        session,
        job_id,
        "INFO",
        "import_rollback_started",
        message=f"Rolling back {total} recorded import actions.",
        action_count=total,
    )
    idx = 0
    last_progress_emit_at = monotonic()
    async for action in _iter_completed_rollback_actions(session, job_id):
        await log_event(
            session,
            job_id,
            "DEBUG",
            "import_rollback_action_started",
            message=(
                f"Rolling back action {idx + 1}/{total}: "
                f"{action.action_type} #{action.sequence_no}."
            ),
            action_index=idx + 1,
            action_count=total,
            action_type=action.action_type,
            sequence_no=action.sequence_no,
        )
        try:
            await rollback_action(session, action)
        except StoryArcManagedPlacementRollbackDeferredError as exc:
            progress = int((idx / max(total, 1)) * 100)
            sync_progress_snapshot_state(
                job,
                status=ImportJobStatus.ROLLING_BACK,
                mode="rollback",
                phase="story_arc_placements",
                progress=progress,
                message="Waiting for an in-progress story-arc placement to stop safely...",
            )
            snapshot = dict(job.progress_snapshot or {})
            snapshot["story_arc_rollback_waiting_work_id"] = exc.work_id
            job.progress_snapshot = snapshot
            job.story_arc_rollback_waiting_work_id = exc.work_id
            await session.flush()
            await log_event(
                session,
                job_id,
                "INFO",
                "import_rollback_waiting_for_story_arc_placement",
                message="Rollback is waiting for in-progress story-arc placement work.",
                action_index=idx + 1,
                action_count=total,
                action_type=action.action_type,
                sequence_no=action.sequence_no,
                sync_work_id=exc.work_id,
            )
            await session.commit()
            return False
        except Exception as exc:
            await log_event(
                session,
                job_id,
                "ERROR",
                "import_rollback_action_failed",
                message=(f"Rollback action failed: {action.action_type} #{action.sequence_no}."),
                action_index=idx + 1,
                action_count=total,
                action_type=action.action_type,
                sequence_no=action.sequence_no,
                error=str(exc),
            )
            raise
        await log_event(
            session,
            job_id,
            "DEBUG",
            "import_rollback_action_completed",
            message=(
                f"Rolled back action {idx + 1}/{total}: {action.action_type} #{action.sequence_no}."
            ),
            action_index=idx + 1,
            action_count=total,
            action_type=action.action_type,
            sequence_no=action.sequence_no,
        )
        idx += 1
        page_checkpoint = idx % ROLLBACK_ACTION_PAGE_SIZE == 0 or idx == total
        if not page_checkpoint:
            continue
        now = monotonic()
        should_emit_progress = progress_callback is not None and (
            idx == total or now - last_progress_emit_at >= ROLLBACK_PROGRESS_MIN_INTERVAL_SECONDS
        )
        if should_emit_progress:
            assert progress_callback is not None
            progress = int((idx / max(total, 1)) * 100)
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job.id,
                    status=ImportJobStatus.ROLLING_BACK,
                    phase="rollback",
                    progress=progress,
                    message=f"Rolling back {idx}/{total} actions...",
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        job.import_started_at,
                        progress,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            last_progress_emit_at = now
        else:
            await session.commit()

    await restore_review_state(session, job_id)

    await recompute_series_counters(session, job)
    await recompute_file_counters(session, job)
    cancelled_during_rollback = job.control_request == ImportControlRequest.CANCEL
    job.status = (
        ImportJobStatus.CANCELLED if cancelled_during_rollback else ImportJobStatus.ROLLED_BACK
    )
    job.control_request = ImportControlRequest.NONE
    job.error_message = "Import cancelled by user." if cancelled_during_rollback else None
    job.progress_snapshot = {}
    job.story_arc_placement_followup_pending = False
    job.story_arc_rollback_waiting_work_id = None
    await session.flush()
    await log_event(
        session,
        job_id,
        "INFO",
        "import_cancelled_after_rollback"
        if cancelled_during_rollback
        else "import_rollback_completed",
        message=(
            "Import cancellation rollback completed."
            if cancelled_during_rollback
            else f"Rollback completed for {total} recorded actions."
        ),
        action_count=total,
    )
    return True
