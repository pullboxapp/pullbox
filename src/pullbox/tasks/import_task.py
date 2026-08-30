"""Dedicated durable runner for collection imports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.database import get_session_factory
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_workflow_state import (
    emit_progress,
    import_control_state_for_job,
    initialize_progress_snapshot,
    paused_message_for_mode,
    snapshot_mode_for_job,
    sync_paused_job_state,
    sync_stalled_job_state,
)
from pullbox.utilities.sse import publish

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_COMPAT_QUEUE_MAXSIZE = 200
_BULK_SAFETY_REMATCH_PAGE_SIZE = 25

_import_progress_queues: dict[int, asyncio.Queue[ImportProgressEvent]] = {}
_latest_import_progress_events: dict[int, ImportProgressEvent] = {}
_latest_import_progress_revisions: dict[int, int] = {}
_import_runner: ImportRunner | None = None
_background_tasks: set[asyncio.Task[Any]] = set()
_review_rematch_locks: dict[int, asyncio.Lock] = {}

_SCAN_STATES = frozenset(
    {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
    }
)
_RECOVERABLE_STATES = frozenset(
    {
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }
)
_TERMINAL_STATES = frozenset(
    {
        ImportJobStatus.REVIEW,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
)
_STARTUP_RECOVERY_PAUSE_REASON = "startup_recovery"
_STARTUP_RECOVERY_DISPATCH_PENDING = "startup_recovery_dispatch_pending"
_RECOVERED_SCAN_PHASE_BY_STATUS = {
    ImportJobStatus.SCANNING: "scanning",
    ImportJobStatus.ANALYZING: "analyzing",
    ImportJobStatus.MATCHING: "matching",
    ImportJobStatus.FILE_MATCHING: "file_matching",
}


def get_progress_queue(job_id: int) -> asyncio.Queue[ImportProgressEvent]:
    """Compatibility queue for tests and route hydration helpers."""
    if job_id not in _import_progress_queues:
        _import_progress_queues[job_id] = asyncio.Queue(maxsize=_COMPAT_QUEUE_MAXSIZE)
    return _import_progress_queues[job_id]


def set_latest_progress_event(event: ImportProgressEvent) -> None:
    """Store the most recent progress event for snapshot hydration."""
    _latest_import_progress_events[event.job_id] = event
    if event.progress_revision > 0:
        _latest_import_progress_revisions[event.job_id] = max(
            _latest_import_progress_revisions.get(event.job_id, 0),
            int(event.progress_revision),
        )


def get_latest_progress_event(job_id: int) -> ImportProgressEvent | None:
    """Return the latest cached progress event for a job."""
    return _latest_import_progress_events.get(job_id)


def get_highest_visible_progress_revision(job_id: int) -> int:
    """Return the highest progress revision published to clients for a job."""
    return _latest_import_progress_revisions.get(job_id, 0)


def remove_progress_queue(job_id: int) -> None:
    """Remove compatibility queue state for a finished import job."""
    _import_progress_queues.pop(job_id, None)
    _latest_import_progress_events.pop(job_id, None)
    _latest_import_progress_revisions.pop(job_id, None)


def purge_import_runtime_state(job_id: int) -> None:
    """Clear all in-process runtime state associated with one import job."""
    remove_progress_queue(job_id)


def _highest_visible_progress_revision(job_id: int, job: ImportJob) -> int:
    """Return the highest revision a client may already have seen for this job."""
    snapshot = dict(job.progress_snapshot or {})
    snapshot_revision = int(snapshot.get("progress_revision") or 0)
    latest_event = get_latest_progress_event(job_id)
    latest_event_revision = int(latest_event.progress_revision or 0) if latest_event else 0
    return max(
        int(job.progress_revision or 0),
        snapshot_revision,
        latest_event_revision,
        get_highest_visible_progress_revision(job_id),
    )


async def _publish_progress_event(event: ImportProgressEvent) -> None:
    """Broadcast an import progress event to SSE subscribers and test queues."""
    visible_revision = get_highest_visible_progress_revision(event.job_id)
    incoming_revision = int(event.progress_revision or 0)
    latest_durable_event = get_latest_progress_event(event.job_id)

    if event.ephemeral_progress:
        if latest_durable_event is not None and latest_durable_event.status in _TERMINAL_STATES:
            return
        if incoming_revision > 0 and incoming_revision <= visible_revision:
            return
    elif incoming_revision <= visible_revision:
        event.progress_revision = visible_revision + 1
        incoming_revision = int(event.progress_revision)

    if incoming_revision > 0:
        _latest_import_progress_revisions[event.job_id] = max(
            visible_revision,
            incoming_revision,
        )

    if not event.ephemeral_progress:
        set_latest_progress_event(event)
        queue = _import_progress_queues.get(event.job_id)
        if queue is not None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("import_progress_compat_queue_full", job_id=event.job_id)
    await publish(
        f"import:{event.job_id}",
        "progress",
        event.model_dump(mode="json"),
    )


def _fire_and_forget(coro: Any) -> None:
    """Retain a background task reference until completion."""
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _emit_terminal_event_for_job(
    session: AsyncSession,
    job_id: int,
) -> ImportJobStatus | None:
    """Publish a truthful terminal event from the durable snapshot."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        return None
    if job.status not in _TERMINAL_STATES:
        return await _publish_current_snapshot_event_for_job(session, job_id)

    terminal_mode = snapshot_mode_for_job(job)
    terminal_phase = "done"
    if job.status == ImportJobStatus.REVIEW:
        terminal_phase = "review"
        terminal_mode = "scan"
    elif job.status == ImportJobStatus.ROLLED_BACK:
        terminal_phase = "rollback"
        terminal_mode = "rollback"
    elif job.status == ImportJobStatus.COMPLETED:
        terminal_mode = "import"

    highest_visible_revision = _highest_visible_progress_revision(job_id, job)
    terminal_revision = highest_visible_revision + 1
    job.progress_revision = terminal_revision

    payload = {
        "job_id": job_id,
        "status": job.status.value,
        "snapshot_version": 2,
        "mode": terminal_mode,
        "phase": terminal_phase,
        "progress": 100,
        "message": job.error_message
        or {
            ImportJobStatus.REVIEW: "Ready for review",
            ImportJobStatus.CANCELLED: "Import cancelled by user.",
            ImportJobStatus.COMPLETED: "Import complete.",
            ImportJobStatus.ROLLED_BACK: "Import rollback completed.",
            ImportJobStatus.FAILED: "Import failed.",
        }.get(job.status, "Task finished"),
        "requested_action": ImportControlRequest.NONE.value,
        "progress_revision": terminal_revision,
        "last_checkpoint_at": datetime.now(UTC).isoformat(),
        "current_series_id": None,
        "current_series_name": None,
        "current_file_id": None,
        "current_file_name": None,
        "current_file_stage": None,
        "current_file_progress_current": None,
        "current_file_progress_total": None,
        "current_file_progress_pct": None,
        "current_file_progress_unit": None,
        "current_series": None,
        "current_series_status": None,
        "estimated_seconds_remaining": None,
        "error_message": job.error_message,
        "control_state": import_control_state_for_job(job),
    }
    snapshot = dict(job.progress_snapshot or {})
    for field_name in (
        "scan_total_files",
        "scan_total_dirs",
        "series_found",
        "series_duplicate",
        "series_matched",
        "series_no_match",
        "series_new",
        "series_imported",
        "series_failed",
        "total_files_found",
        "total_files_matched",
        "total_files_duplicate",
        "total_files_already_owned",
        "total_files_conflict",
        "total_files_no_match",
        "total_files_imported",
        "total_files_failed",
        "story_arc_placements_total",
        "story_arc_placements_queued",
        "story_arc_placements_running",
        "story_arc_placements_retry_wait",
        "story_arc_placements_failed",
        "story_arc_placements_completed",
        "story_arc_placements_cancelled",
        "review_summary",
        "scan_started_at",
        "import_started_at",
        "recent_logs",
    ):
        payload[field_name] = snapshot.get(field_name)

    event = ImportProgressEvent.model_validate(payload)
    await emit_progress(
        session,
        job,
        event,
        progress_callback=_publish_progress_event,
    )
    return job.status


async def _publish_current_snapshot_event_for_job(
    session: AsyncSession,
    job_id: int,
) -> ImportJobStatus | None:
    """Publish the current durable snapshot without coercing it into a terminal state."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        return None

    snapshot = dict(job.progress_snapshot or {})
    highest_visible_revision = _highest_visible_progress_revision(job_id, job)
    published_revision = max(
        highest_visible_revision + 1,
        int(snapshot.get("progress_revision") or job.progress_revision or 0),
    )
    job.progress_revision = published_revision
    if snapshot:
        snapshot["progress_revision"] = published_revision

    event = ImportProgressEvent.model_validate(
        {
            "job_id": job_id,
            "status": job.status.value,
            "snapshot_version": int(snapshot.get("snapshot_version") or 2),
            "mode": snapshot_mode_for_job(job),
            "phase": snapshot.get("phase") or "inventory",
            "progress": int(snapshot.get("progress") or 0),
            "message": snapshot.get("message")
            or (
                paused_message_for_mode(snapshot_mode_for_job(job))
                if job.status == ImportJobStatus.PAUSED
                else "Task updated"
            ),
            "requested_action": ImportControlRequest.NONE.value,
            "progress_revision": published_revision,
            "last_checkpoint_at": snapshot.get("last_checkpoint_at"),
            "current_series_id": snapshot.get("current_series_id"),
            "current_series_name": snapshot.get("current_series_name")
            or snapshot.get("current_series"),
            "current_file_id": snapshot.get("current_file_id"),
            "current_file_name": snapshot.get("current_file_name"),
            "current_file_stage": snapshot.get("current_file_stage"),
            "current_file_progress_current": snapshot.get("current_file_progress_current"),
            "current_file_progress_total": snapshot.get("current_file_progress_total"),
            "current_file_progress_pct": snapshot.get("current_file_progress_pct"),
            "current_file_progress_unit": snapshot.get("current_file_progress_unit"),
            "current_series": snapshot.get("current_series") or snapshot.get("current_series_name"),
            "estimated_seconds_remaining": snapshot.get("estimated_seconds_remaining"),
            "story_arc_placements_total": snapshot.get("story_arc_placements_total"),
            "story_arc_placements_queued": snapshot.get("story_arc_placements_queued"),
            "story_arc_placements_running": snapshot.get("story_arc_placements_running"),
            "story_arc_placements_retry_wait": snapshot.get("story_arc_placements_retry_wait"),
            "story_arc_placements_failed": snapshot.get("story_arc_placements_failed"),
            "story_arc_placements_completed": snapshot.get("story_arc_placements_completed"),
            "story_arc_placements_cancelled": snapshot.get("story_arc_placements_cancelled"),
            "error_message": job.error_message,
            "control_state": import_control_state_for_job(job),
        }
    )
    await emit_progress(
        session,
        job,
        event,
        progress_callback=_publish_progress_event,
    )
    return job.status


async def _build_import_service(session: AsyncSession) -> Any:
    """Build an ImportService with full dependencies."""
    from pullbox.composition.services import build_import_service

    return await build_import_service(session)


def _should_schedule_comicinfo_enrichment(result: Any) -> bool:
    """Return True only for the explicit run_import follow-up signal."""
    return getattr(result, "schedule_comicinfo_enrichment", False) is True


def _should_schedule_story_arc_sync(result: Any) -> bool:
    """Return True only for a committed import-owned placement batch."""
    return getattr(result, "schedule_story_arc_sync", False) is True


def _rollback_waits_for_story_arc(result: Any) -> bool:
    """Return True only for the explicit cooperative rollback deferral signal."""
    return result is False


async def publish_story_arc_import_updates(
    job_ids: tuple[int, ...],
    *,
    completed_job_ids: tuple[int, ...] = (),
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Publish reconciled import state and start post-commit completion work."""
    factory = session_factory or get_session_factory()
    completed = frozenset(completed_job_ids)
    completed_for_dispatch = False
    for job_id in dict.fromkeys(job_ids):
        async with factory() as session:
            job = await session.get(ImportJob, job_id)
            if job is None:
                continue
            service: Any | None = None
            followup_pending = bool(
                job_id in completed
                and job.status is ImportJobStatus.COMPLETED
                and job.story_arc_placement_followup_pending
            )
            if followup_pending:
                service = await _build_import_service(session)
                await session.commit()
                service.schedule_comicinfo_enrichment(factory, job_id=job_id)

            terminal_status = await _emit_terminal_event_for_job(session, job_id)
            if followup_pending and terminal_status is ImportJobStatus.COMPLETED:
                refreshed = await session.get(ImportJob, job_id)
                if refreshed is not None:
                    snapshot = dict(refreshed.progress_snapshot or {})
                    snapshot["story_arc_placement_followup_pending"] = False
                    refreshed.progress_snapshot = snapshot
                    refreshed.story_arc_placement_followup_pending = False
            await session.commit()
            completed_for_dispatch = completed_for_dispatch or (
                terminal_status is ImportJobStatus.COMPLETED
            )
            if terminal_status in _TERMINAL_STATES:
                purge_import_runtime_state(job_id)
    if completed_for_dispatch and _import_runner is not None:
        await _import_runner.request_recovered_dispatch()


class ImportRunner:
    """Single-import durable runner with startup recovery and broadcast progress."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._active_job_id: int | None = None
        self._dispatch_recovered_requested = False

    async def recover_and_dispatch(self) -> int:
        """Recover interrupted imports and resume any runnable job."""
        recovered = await recover_stuck_import_jobs(self._session_factory)
        await self._dispatch_recovered_job()
        return recovered

    async def request_scan(self, job_id: int) -> None:
        """Run or resume a scan-phase import job."""
        await self._start_if_idle(job_id)

    async def request_execute(self, job_id: int) -> None:
        """Run or resume an execute-phase import job."""
        await self._start_if_idle(job_id)

    async def request_resume(self, job_id: int) -> None:
        """Resume a paused import job."""
        await self._start_if_idle(job_id)

    async def request_rollback(self, job_id: int) -> None:
        """Run a rollback for the given import job."""
        await self._start_if_idle(job_id)

    async def request_recovered_dispatch(self) -> None:
        """Resume the next startup-recovered job once the runner is idle."""
        await self._dispatch_recovered_job()

    async def _dispatch_recovered_job(self) -> None:
        async with self._lock:
            if self._worker_task is not None and not self._worker_task.done():
                self._dispatch_recovered_requested = True
                return
            self._dispatch_recovered_requested = False
            job_id = await self._resume_next_recovered_job()
            if job_id is not None:
                self._start_worker_locked(job_id)

    async def _resume_next_recovered_job(self) -> int | None:
        """Restore and return the oldest runnable startup-recovered job."""
        async with self._session_factory() as session:
            stalled_job_id = await session.scalar(
                sa_select(ImportJob.id)
                .where(ImportJob.status == ImportJobStatus.STALLED)
                .order_by(ImportJob.created_at.asc(), ImportJob.id.asc())
                .limit(1)
            )
            if stalled_job_id is not None:
                logger.info(
                    "import_recovered_dispatch_blocked_by_stalled_job",
                    job_id=int(stalled_job_id),
                )
                return None
            result = await session.execute(
                sa_select(ImportJob)
                .where(
                    ImportJob.status.in_(
                        {
                            ImportJobStatus.ROLLING_BACK,
                            ImportJobStatus.PAUSED,
                        }
                    )
                )
                .order_by(ImportJob.created_at.asc(), ImportJob.id.asc())
            )
            for job in result.scalars().all():
                if job.status == ImportJobStatus.PAUSED:
                    if not _is_startup_recovered_pause(job):
                        continue
                    _resume_startup_recovered_job(job)
                    await session.commit()
                    logger.info("import_recovered_job_auto_resumed", job_id=job.id)
                    return int(job.id)
                if not _is_startup_recovered_rollback(job):
                    continue
                snapshot = dict(job.progress_snapshot or {})
                snapshot.pop(_STARTUP_RECOVERY_DISPATCH_PENDING, None)
                snapshot.pop("recovered_status", None)
                snapshot.pop("recovered_at", None)
                job.progress_snapshot = snapshot
                await session.commit()
                logger.info("import_recovered_rollback_auto_resumed", job_id=job.id)
                return int(job.id)
        return None

    async def _start_if_idle(self, job_id: int) -> None:
        async with self._lock:
            if self._worker_task is not None and not self._worker_task.done():
                if self._active_job_id != job_id:
                    logger.info(
                        "import_runner_already_active",
                        active_job_id=self._active_job_id,
                        requested_job_id=job_id,
                    )
                return

            self._start_worker_locked(job_id)

    def _start_worker_locked(self, job_id: int) -> None:
        """Start one serial worker while the runner lock is held."""
        self._active_job_id = job_id
        self._worker_task = asyncio.create_task(self._run_job_sequence(job_id))
        self._worker_task.add_done_callback(self._on_worker_done)

    async def _run_job_sequence(self, job_id: int) -> None:
        """Drain recovered jobs serially while each predecessor reaches an idle state."""
        current_job_id: int | None = job_id
        while current_job_id is not None:
            self._active_job_id = current_job_id
            await self._run_job(current_job_id)
            if not await self._job_is_terminal_or_deleted(current_job_id):
                return
            current_job_id = await self._resume_next_recovered_job()

    async def _job_is_terminal_or_deleted(self, job_id: int) -> bool:
        async with self._session_factory() as session:
            job = await session.get(ImportJob, job_id)
            return job is None or job.status in _TERMINAL_STATES

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("import_runner_worker_failed")
        finally:
            if self._worker_task is task:
                self._worker_task = None
                self._active_job_id = None
                if self._dispatch_recovered_requested:
                    self._dispatch_recovered_requested = False
                    _fire_and_forget(self._dispatch_recovered_job())

    async def _mark_paused(self, session: AsyncSession, job_id: int) -> None:
        job = await session.get(ImportJob, job_id)
        if job is None:
            return
        if job.status != ImportJobStatus.CANCELLED:
            sync_paused_job_state(job)
            highest_visible_revision = _highest_visible_progress_revision(job_id, job)
            paused_revision = highest_visible_revision + 1
            job.progress_revision = paused_revision
            snapshot = dict(job.progress_snapshot or {})
            snapshot["progress_revision"] = paused_revision
            job.progress_snapshot = snapshot
            await session.commit()

    async def _finalize_cancel(self, session: AsyncSession, job_id: int) -> None:
        job = await session.get(ImportJob, job_id)
        if job is None:
            return
        if job.import_started_at is None:
            await session.delete(job)
            await session.commit()
            purge_import_runtime_state(job_id)
            return
        job.status = ImportJobStatus.ROLLING_BACK
        job.control_request = ImportControlRequest.CANCEL
        job.error_message = job.error_message or "Import cancelled by user."
        job.progress_snapshot = initialize_progress_snapshot(
            job,
            mode="rollback",
            phase="queued",
            progress=0,
            message="Cancelling import and rolling back changes...",
            status=ImportJobStatus.ROLLING_BACK,
        )
        highest_visible_revision = _highest_visible_progress_revision(job_id, job)
        rollback_revision = highest_visible_revision + 1
        job.progress_revision = rollback_revision
        snapshot = dict(job.progress_snapshot or {})
        snapshot["progress_revision"] = rollback_revision
        job.progress_snapshot = snapshot
        await session.commit()

    async def _mutate_job_with_retries(
        self,
        job_id: int,
        *,
        mutate: Callable[[ImportJob], None],
        log_event: str,
    ) -> None:
        """Persist a runner recovery mutation through a fresh session."""
        for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
            try:
                async with self._session_factory() as recovery_session:
                    job = await recovery_session.get(ImportJob, job_id)
                    if job is None:
                        return
                    mutate(job)
                    await recovery_session.commit()
                return
            except Exception as exc:
                if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    logger.exception(log_event, job_id=job_id)
                    return
                await asyncio.sleep(sqlite_lock_retry_delay(attempt))

    async def _mark_stalled(self, job_id: int) -> None:
        def _mutate(job: ImportJob) -> None:
            if job.status in _TERMINAL_STATES:
                return
            sync_stalled_job_state(job)
            snapshot = dict(job.progress_snapshot or {})
            snapshot["stalled_reason"] = "database_locked"
            snapshot["stalled_at"] = datetime.now(UTC).isoformat()
            job.progress_snapshot = snapshot

        await self._mutate_job_with_retries(
            job_id,
            mutate=_mutate,
            log_event="import_runner_stalled_persist_failed",
        )

    async def _mark_failed(self, job_id: int) -> None:
        def _mutate(job: ImportJob) -> None:
            if job.status in _TERMINAL_STATES:
                return
            job.status = ImportJobStatus.FAILED
            if not job.error_message:
                job.error_message = "Import worker failed. Check logs for details."

        await self._mutate_job_with_retries(
            job_id,
            mutate=_mutate,
            log_event="import_runner_failed_persist_failed",
        )

    async def _publish_final_state(self, job_id: int) -> ImportJobStatus | None:
        """Publish the current final worker state from a clean session."""
        for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
            try:
                async with self._session_factory() as final_session:
                    job = await final_session.get(ImportJob, job_id)
                    if job is not None and job.status in {
                        ImportJobStatus.PAUSED,
                        ImportJobStatus.STALLED,
                    }:
                        return await _publish_current_snapshot_event_for_job(
                            final_session,
                            job_id,
                        )
                    return await _emit_terminal_event_for_job(final_session, job_id)
            except Exception as exc:
                if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    logger.exception("import_runner_final_event_failed", job_id=job_id)
                    return None
                await asyncio.sleep(sqlite_lock_retry_delay(attempt))
        return None

    async def _run_job(self, job_id: int) -> None:
        async with self._session_factory() as session:
            service = await _build_import_service(session)

            async def progress_callback(event: ImportProgressEvent) -> None:
                await _publish_progress_event(event)

            try:
                job = await session.get(ImportJob, job_id)
                if job is None:
                    return

                if job.status == ImportJobStatus.STALLED:
                    _resume_stalled_job(job)
                    await session.commit()

                if job.status in _SCAN_STATES:
                    await service.resume_scan_phase(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                    await session.commit()
                elif job.status == ImportJobStatus.IMPORTING:
                    result = await service.run_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                    await session.commit()
                    if _should_schedule_story_arc_sync(result):
                        service.schedule_story_arc_sync()
                    if _should_schedule_comicinfo_enrichment(result):
                        service.schedule_comicinfo_enrichment(
                            self._session_factory,
                            job_id=job_id,
                        )
                elif job.status == ImportJobStatus.ROLLING_BACK:
                    rollback_result = await service.rollback_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                    await session.commit()
                    if _rollback_waits_for_story_arc(rollback_result):
                        service.schedule_story_arc_sync()
                else:
                    logger.info("import_runner_noop", job_id=job_id, status=job.status.value)
                    return
            except JobPausedError:
                await session.rollback()
                await self._mark_paused(session, job_id)
            except JobCancelledError:
                await session.rollback()
                await self._finalize_cancel(session, job_id)
                job = await session.get(ImportJob, job_id)
                if job is not None and job.status == ImportJobStatus.ROLLING_BACK:
                    rollback_result = await service.rollback_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                    await session.commit()
                    if _rollback_waits_for_story_arc(rollback_result):
                        service.schedule_story_arc_sync()
            except Exception as exc:
                logger.exception("import_runner_job_failed", job_id=job_id)
                await session.rollback()
                if is_sqlite_locked_error(exc):
                    await self._mark_stalled(job_id)
                else:
                    await self._mark_failed(job_id)
            finally:
                terminal_status = await self._publish_final_state(job_id)
                if terminal_status in {
                    ImportJobStatus.CANCELLED,
                    ImportJobStatus.COMPLETED,
                    ImportJobStatus.FAILED,
                    ImportJobStatus.ROLLED_BACK,
                }:
                    purge_import_runtime_state(job_id)


def get_import_runner() -> ImportRunner:
    """Return the singleton import runner."""
    global _import_runner
    if _import_runner is None:
        _import_runner = ImportRunner(get_session_factory())
    return _import_runner


def set_import_runner(runner: ImportRunner) -> None:
    """Install the singleton import runner (primarily for startup/tests)."""
    global _import_runner
    _import_runner = runner


async def _run_single_job_once(
    job_id: int,
    *,
    expected_statuses: set[ImportJobStatus] | None = None,
) -> None:
    """Compatibility worker entrypoint for legacy one-shot task tests."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = await _build_import_service(session)
        terminal_event_override: ImportProgressEvent | None = None
        run_import_result: Any = None
        rollback_result: Any = None

        async def progress_callback(event: ImportProgressEvent) -> None:
            await _publish_progress_event(event)

        try:
            job = await session.get(ImportJob, job_id)
            if job is None:
                return
            if expected_statuses is not None and job.status not in expected_statuses:
                logger.info(
                    "import_task_noop",
                    job_id=job_id,
                    status=job.status.value,
                    expected_statuses=[status.value for status in expected_statuses],
                )
                return

            if job.status == ImportJobStatus.CANCELLED:
                raise JobCancelledError("cancelled")
            if job.status in _SCAN_STATES:
                await service.start_scan(session, job_id, progress_callback=progress_callback)
            elif job.status in {ImportJobStatus.IMPORTING, ImportJobStatus.ROLLING_BACK}:
                if job.status == ImportJobStatus.ROLLING_BACK:
                    rollback_result = await service.rollback_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                else:
                    run_import_result = await service.run_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
            else:
                logger.info("import_task_noop", job_id=job_id, status=job.status.value)
                return

            await session.commit()
            if _should_schedule_story_arc_sync(run_import_result):
                service.schedule_story_arc_sync()
            if _rollback_waits_for_story_arc(rollback_result):
                service.schedule_story_arc_sync()
            if _should_schedule_comicinfo_enrichment(run_import_result):
                service.schedule_comicinfo_enrichment(session_factory, job_id=job_id)
        except JobPausedError:
            await session.rollback()
            job = await session.get(ImportJob, job_id)
            if job is not None and job.status != ImportJobStatus.CANCELLED:
                sync_paused_job_state(job)
                await session.commit()
        except JobCancelledError:
            await session.rollback()
            job = await session.get(ImportJob, job_id)
            if job is not None:
                if job.import_started_at is None:
                    terminal_event_override = ImportProgressEvent(
                        job_id=job_id,
                        status=ImportJobStatus.CANCELLED,
                        phase="done",
                        progress=100,
                        message=job.error_message or "Import cancelled by user.",
                    )
                    await session.delete(job)
                    await session.commit()
                else:
                    job.status = ImportJobStatus.ROLLING_BACK
                    job.control_request = ImportControlRequest.CANCEL
                    job.error_message = job.error_message or "Import cancelled by user."
                    job.progress_snapshot = initialize_progress_snapshot(
                        job,
                        mode="rollback",
                        phase="queued",
                        progress=0,
                        message="Cancelling import and rolling back changes...",
                        status=ImportJobStatus.ROLLING_BACK,
                    )
                    await session.commit()
                    rollback_result = await service.rollback_import(
                        session,
                        job_id,
                        progress_callback=progress_callback,
                    )
                    await session.commit()
                    if _rollback_waits_for_story_arc(rollback_result):
                        service.schedule_story_arc_sync()
        except Exception:
            logger.exception("import_task_failed", job_id=job_id)
            await session.rollback()
            job = await session.get(ImportJob, job_id)
            if job is not None and job.status not in _TERMINAL_STATES:
                job.status = ImportJobStatus.FAILED
                if not job.error_message:
                    job.error_message = "Import worker failed. Check logs for details."
                await session.commit()
        finally:
            terminal_status: ImportJobStatus | None
            if terminal_event_override is not None:
                await _publish_progress_event(terminal_event_override)
                terminal_status = terminal_event_override.status
            else:
                job = await session.get(ImportJob, job_id)
                if job is not None and job.status == ImportJobStatus.PAUSED:
                    terminal_status = await _publish_current_snapshot_event_for_job(
                        session,
                        job_id,
                    )
                else:
                    terminal_status = await _emit_terminal_event_for_job(session, job_id)
            if terminal_status is not None and terminal_status in {
                ImportJobStatus.REVIEW,
                ImportJobStatus.CANCELLED,
                ImportJobStatus.COMPLETED,
                ImportJobStatus.FAILED,
                ImportJobStatus.ROLLED_BACK,
            }:
                purge_import_runtime_state(job_id)


async def run_import_scan_task(job_id: int) -> None:
    """Legacy one-shot scan task kept for test compatibility."""
    await _run_single_job_once(
        job_id,
        expected_statuses=set(_SCAN_STATES) | {ImportJobStatus.CANCELLED},
    )


async def run_import_execute_task(job_id: int) -> None:
    """Legacy one-shot execute task kept for test compatibility."""
    await _run_single_job_once(job_id, expected_statuses={ImportJobStatus.IMPORTING})


async def run_import_series_rematch_task(job_id: int, imported_series_id: int) -> None:
    """Rerun review file matching for one manually overridden import series."""
    lock = _review_rematch_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        await _run_import_series_rematch_task(job_id, imported_series_id)


async def run_import_safety_bulk_rematch_task(job_id: int) -> None:
    """Rematch safety-approved series from one job in bounded keyset pages."""
    lock = _review_rematch_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        session_factory = get_session_factory()
        cursor = 0
        while True:
            async with session_factory() as session:
                job = await session.get(ImportJob, job_id)
                if (
                    job is None
                    or job.status != ImportJobStatus.REVIEW
                    or job.control_request != ImportControlRequest.NONE
                ):
                    return
                series_ids = list(
                    (
                        await session.execute(
                            sa_select(ImportedSeries.id)
                            .join(
                                ImportedFile,
                                ImportedFile.import_series_id == ImportedSeries.id,
                            )
                            .where(
                                ImportedSeries.import_job_id == job_id,
                                ImportedSeries.id > cursor,
                                ImportedSeries.diagnostics["rematch_pending"]
                                .as_boolean()
                                .is_(True),
                                ImportedFile.import_job_id == job_id,
                                ImportedFile.status == ImportedFileStatus.SAFETY_APPROVED,
                            )
                            .distinct()
                            .order_by(ImportedSeries.id)
                            .limit(_BULK_SAFETY_REMATCH_PAGE_SIZE)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not series_ids:
                return
            for imported_series_id in series_ids:
                await _run_import_series_rematch_task(job_id, imported_series_id)
                cursor = imported_series_id


async def _run_import_series_rematch_task(job_id: int, imported_series_id: int) -> None:
    """Perform one rematch after the job-level review rematch lock is held."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = await _build_import_service(session)
        try:
            await service.rematch_imported_series_files(session, job_id, imported_series_id)
            await session.commit()
        except Exception:
            logger.exception(
                "import_series_rematch_failed",
                job_id=job_id,
                imported_series_id=imported_series_id,
            )
            await session.rollback()
            try:
                item = await session.get(ImportedSeries, imported_series_id)
                if item is not None:
                    diagnostics = dict(item.diagnostics or {})
                    diagnostics.pop("rematch_pending", None)
                    diagnostics["rematch_error"] = "File rematch failed. Check the import log."
                    item.diagnostics = diagnostics
                    files_result = await session.execute(
                        sa_select(ImportedFile).where(
                            ImportedFile.import_job_id == job_id,
                            ImportedFile.import_series_id == imported_series_id,
                            ImportedFile.status == ImportedFileStatus.SAFETY_APPROVED,
                        )
                    )
                    for imp_file in files_result.scalars().all():
                        imp_file.status = ImportedFileStatus.SAFETY_BLOCKED
                        imp_file.error_message = "File rematch failed. Check the import log."
                if await session.get(ImportJob, job_id) is not None:
                    await service._log_event(
                        session,
                        job_id,
                        "ERROR",
                        "import_series_file_rematch_failed",
                        message="File rematch failed after applying ComicVine override",
                        imported_series_id=imported_series_id,
                    )
                await session.commit()
            except Exception:
                logger.exception(
                    "import_series_rematch_failure_persist_failed",
                    job_id=job_id,
                    imported_series_id=imported_series_id,
                )
                await session.rollback()


def trigger_import_scan(job_id: int) -> None:
    """Schedule an import scan on the durable runner."""
    _fire_and_forget(get_import_runner().request_scan(job_id))
    logger.info("import_scan_triggered", job_id=job_id)


def trigger_import_execute(job_id: int) -> None:
    """Schedule import execution on the durable runner."""
    _fire_and_forget(get_import_runner().request_execute(job_id))
    logger.info("import_execute_triggered", job_id=job_id)


def trigger_import_resume(job_id: int) -> None:
    """Resume a paused import job on the durable runner."""
    _fire_and_forget(get_import_runner().request_resume(job_id))
    logger.info("import_resume_triggered", job_id=job_id)


def trigger_import_rollback(job_id: int) -> None:
    """Schedule import rollback on the durable runner."""
    _fire_and_forget(get_import_runner().request_rollback(job_id))
    logger.info("import_rollback_triggered", job_id=job_id)


def trigger_import_series_rematch(job_id: int, imported_series_id: int) -> None:
    """Schedule a single-series import review rematch."""
    _fire_and_forget(run_import_series_rematch_task(job_id, imported_series_id))
    logger.info(
        "import_series_rematch_triggered",
        job_id=job_id,
        imported_series_id=imported_series_id,
    )


def trigger_import_safety_bulk_rematch(job_id: int) -> None:
    """Schedule one bounded worker for all pending safety rematches in a job."""
    _fire_and_forget(run_import_safety_bulk_rematch_task(job_id))
    logger.info("import_safety_bulk_rematch_triggered", job_id=job_id)


async def recover_stuck_import_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Pause interrupted imports on startup so they can be resumed safely."""
    async with session_factory() as session:
        result = await session.execute(
            sa_select(ImportJob).where(ImportJob.status.in_(_RECOVERABLE_STATES))
        )
        jobs = list(result.scalars().all())
        if not jobs:
            return 0

        for job in jobs:
            previous_status = job.status
            if previous_status in {
                ImportJobStatus.IMPORTING,
                ImportJobStatus.CANCELLING,
                ImportJobStatus.ROLLING_BACK,
            }:
                series_result = await session.execute(
                    sa_select(ImportedSeries).where(
                        ImportedSeries.import_job_id == job.id,
                        ImportedSeries.status == ImportSeriesStatus.IMPORTING,
                    )
                )
                for item in series_result.scalars().all():
                    item.status = ImportSeriesStatus.CONFIRMED

            if (
                job.control_request is ImportControlRequest.CANCEL
                or previous_status is ImportJobStatus.CANCELLING
            ):
                job.status = ImportJobStatus.ROLLING_BACK
                job.control_request = ImportControlRequest.CANCEL
                job.error_message = job.error_message or "Import cancelled by user."
                job.story_arc_placement_followup_pending = False
                job.progress_snapshot = initialize_progress_snapshot(
                    job,
                    mode="rollback",
                    phase="queued",
                    progress=0,
                    message="Cancelling import and rolling back changes...",
                    status=ImportJobStatus.ROLLING_BACK,
                )
                recovered_snapshot = dict(job.progress_snapshot or {})
                recovered_snapshot[_STARTUP_RECOVERY_DISPATCH_PENDING] = True
                recovered_snapshot["recovered_status"] = previous_status.value
                recovered_snapshot["recovered_at"] = datetime.now(UTC).isoformat()
                job.progress_snapshot = recovered_snapshot
                logger.warning(
                    "import_cancel_recovered_on_startup",
                    job_id=job.id,
                    previous_status=previous_status.value,
                )
                continue

            if (
                job.control_request is ImportControlRequest.PAUSE
                or previous_status is ImportJobStatus.PAUSING
            ):
                sync_paused_job_state(job)
                logger.warning(
                    "import_pause_recovered_on_startup",
                    job_id=job.id,
                    previous_status=previous_status.value,
                )
                continue

            snapshot = dict(job.progress_snapshot or {})
            if previous_status == ImportJobStatus.ROLLING_BACK:
                snapshot["phase"] = "rollback"
            elif previous_status == ImportJobStatus.IMPORTING:
                snapshot.setdefault("phase", "importing")
                snapshot["mode"] = "import"
            else:
                snapshot["mode"] = "scan"
                if previous_status in _RECOVERED_SCAN_PHASE_BY_STATUS:
                    snapshot.setdefault(
                        "phase",
                        _RECOVERED_SCAN_PHASE_BY_STATUS[previous_status],
                    )
            job.progress_snapshot = snapshot
            sync_paused_job_state(job)
            recovered_snapshot = dict(job.progress_snapshot or {})
            recovered_snapshot["pause_reason"] = _STARTUP_RECOVERY_PAUSE_REASON
            recovered_snapshot["recovered_status"] = previous_status.value
            recovered_snapshot["recovered_at"] = datetime.now(UTC).isoformat()
            recovered_snapshot["message"] = (
                "Import was interrupted by restart and will resume automatically."
            )
            job.progress_snapshot = recovered_snapshot
            logger.warning(
                "import_job_recovered_on_startup",
                job_id=job.id,
                previous_status=previous_status.value,
            )

        await session.commit()
        return len(jobs)


def _is_startup_recovered_pause(job: ImportJob) -> bool:
    """Return True when a paused job was paused by startup recovery, not the user."""
    snapshot = dict(job.progress_snapshot or {})
    return snapshot.get("pause_reason") == _STARTUP_RECOVERY_PAUSE_REASON


def _is_startup_recovered_rollback(job: ImportJob) -> bool:
    """Return True only for rollback work durably queued during startup recovery."""
    snapshot = dict(job.progress_snapshot or {})
    return snapshot.get(_STARTUP_RECOVERY_DISPATCH_PENDING) is True


def _resume_startup_recovered_job(job: ImportJob) -> None:
    """Restore the runnable status for a job paused by startup recovery."""
    snapshot = dict(job.progress_snapshot or {})
    phase = str(snapshot.get("phase") or "")
    recovered_status = str(snapshot.get("recovered_status") or "")
    if phase in {"", "inventory"}:
        for status, status_phase in _RECOVERED_SCAN_PHASE_BY_STATUS.items():
            if recovered_status == status.value:
                phase = status_phase
                break
    mode = snapshot_mode_for_job(job)
    if mode == "scan" and job.import_started_at is not None:
        if phase == "importing":
            mode = "import"
        elif phase == "rollback":
            mode = "rollback"

    if mode == "import":
        job.status = ImportJobStatus.IMPORTING
    elif mode == "rollback":
        job.status = ImportJobStatus.ROLLING_BACK
    elif phase == "file_matching":
        job.status = ImportJobStatus.FILE_MATCHING
    elif phase == "matching":
        job.status = ImportJobStatus.MATCHING
    elif phase == "analyzing":
        job.status = ImportJobStatus.ANALYZING
    else:
        job.status = ImportJobStatus.SCANNING

    job.control_request = ImportControlRequest.NONE
    job.error_message = None
    sync_progress_snapshot = dict(snapshot)
    sync_progress_snapshot.pop("pause_reason", None)
    sync_progress_snapshot.pop("recovered_status", None)
    sync_progress_snapshot.pop("recovered_at", None)
    sync_progress_snapshot["status"] = job.status.value
    sync_progress_snapshot["mode"] = mode
    sync_progress_snapshot["phase"] = phase or "scanning"
    sync_progress_snapshot["message"] = "Import recovered after restart; resuming automatically."
    sync_progress_snapshot["requested_action"] = ImportControlRequest.NONE.value
    job.progress_snapshot = sync_progress_snapshot


def _resume_stalled_job(job: ImportJob) -> None:
    """Restore the runnable status for a job stalled by transient DB contention."""
    snapshot = dict(job.progress_snapshot or {})
    phase = str(snapshot.get("phase") or "")
    mode = snapshot_mode_for_job(job)
    if mode == "scan" and job.import_started_at is not None:
        if phase == "importing":
            mode = "import"
        elif phase == "rollback":
            mode = "rollback"

    if mode == "import":
        job.status = ImportJobStatus.IMPORTING
    elif mode == "rollback":
        job.status = ImportJobStatus.ROLLING_BACK
    elif phase == "file_matching":
        job.status = ImportJobStatus.FILE_MATCHING
    elif phase == "matching":
        job.status = ImportJobStatus.MATCHING
    elif phase == "analyzing":
        job.status = ImportJobStatus.ANALYZING
    else:
        job.status = ImportJobStatus.SCANNING

    job.control_request = ImportControlRequest.NONE
    job.error_message = None
    resume_snapshot = dict(snapshot)
    resume_snapshot.pop("stalled_reason", None)
    resume_snapshot.pop("stalled_at", None)
    resume_snapshot["status"] = job.status.value
    resume_snapshot["mode"] = mode
    resume_snapshot["phase"] = phase or "scanning"
    resume_snapshot["message"] = "Import resumed after database contention."
    resume_snapshot["requested_action"] = ImportControlRequest.NONE.value
    job.progress_snapshot = resume_snapshot
