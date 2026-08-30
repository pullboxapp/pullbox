"""Import job lifecycle-control helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.models.import_job import ImportControlRequest, ImportJob, ImportJobStatus
from pullbox.services.import_workflow_state import (
    initialize_progress_snapshot,
    paused_message_for_mode,
    snapshot_mode_for_job,
    sync_paused_job_state,
    sync_progress_snapshot_state,
)
from pullbox.services.story_arc_sync_queue import (
    discard_unpublished_import_story_arc_sync_work,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ImportEventLogger(Protocol):
    """Callable contract for writing structured import-job events."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> Awaitable[None]: ...


JobDeletedLogger = Callable[[int, ImportJobStatus], None]


def _is_story_arc_placement_wait(job: ImportJob) -> bool:
    """Return whether an import is waiting on separately scheduled placements."""
    return (
        job.status == ImportJobStatus.IMPORTING
        and job.import_started_at is not None
        and dict(job.progress_snapshot or {}).get("phase") == "story_arc_placements"
    )


async def _flush_job_with_sqlite_lock_retry(
    session: AsyncSession,
    job: ImportJob,
    *,
    job_id: int,
    mutate: Callable[[ImportJob], None],
) -> ImportJob:
    """Persist a control-state mutation, retrying transient SQLite lock races."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        try:
            await session.flush()
            return job
        except OperationalError as exc:
            await session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(sqlite_lock_retry_delay(attempt))
            reloaded = await session.get(
                ImportJob,
                job_id,
                populate_existing=True,
                with_for_update=True,
            )
            if reloaded is None:
                raise NotFoundError("ImportJob", job_id) from exc
            mutate(reloaded)
            job = reloaded
    return job


async def raise_if_job_cancelled_immediately(
    session: AsyncSession,
    job_id: int,
    *,
    raise_if_cancelled: Callable[[AsyncSession, int], Awaitable[None]],
) -> None:
    """Read control state through a fresh session for killable file operations."""
    bind = session.bind
    if bind is None:
        with session.no_autoflush:
            await raise_if_cancelled(session, job_id)
        return

    bind_url = str(bind.sync_engine.url)
    if ":memory:" in bind_url:
        with session.no_autoflush:
            await raise_if_cancelled(session, job_id)
        return

    # Long archive conversions and rewrites need to see pause/cancel through
    # a fresh transaction snapshot, not whatever the active import session
    # last observed before the subprocess started running.
    fresh_session_factory = async_sessionmaker(
        bind,
        class_=type(session),
        expire_on_commit=False,
    )
    async with fresh_session_factory() as control_session:
        await raise_if_cancelled(control_session, job_id)


async def cancel_job(
    session: AsyncSession,
    job_id: int,
    *,
    log_job_deleted: JobDeletedLogger,
) -> str:
    """Discard a non-running import job or delete a terminal history row."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    deletable = {
        ImportJobStatus.PENDING,
        ImportJobStatus.REVIEW,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }

    if (
        job.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}
        and job.import_started_at is None
    ):
        await discard_unpublished_import_story_arc_sync_work(session, (job_id,))
        await session.delete(job)
        await session.flush()
        return "deleted"

    if job.status in deletable:
        await discard_unpublished_import_story_arc_sync_work(session, (job_id,))
        log_job_deleted(job_id, job.status)
        await session.delete(job)
        await session.flush()
        return "deleted"

    raise ValidationError(f"Job in {job.status} state must use pause/cancel/rollback controls")


async def pause_job(
    session: AsyncSession,
    job_id: int,
    *,
    log_event: ImportEventLogger,
) -> ImportJob:
    """Persist or request a pause at the nearest resumable checkpoint."""
    job = await session.get(
        ImportJob,
        job_id,
        populate_existing=True,
        with_for_update=True,
    )
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    if job.status not in {
        ImportJobStatus.SCANNING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
    }:
        raise ValidationError(f"Cannot pause job in {job.status} state")
    if _is_story_arc_placement_wait(job):
        raise ValidationError("Import cannot be paused while story arc placements are finishing")

    def _apply_pause(target: ImportJob) -> None:
        if target.status not in {
            ImportJobStatus.SCANNING,
            ImportJobStatus.ANALYZING,
            ImportJobStatus.MATCHING,
            ImportJobStatus.FILE_MATCHING,
            ImportJobStatus.IMPORTING,
        }:
            raise ValidationError(f"Cannot pause job in {target.status} state")
        if _is_story_arc_placement_wait(target):
            raise ValidationError(
                "Import cannot be paused while story arc placements are finishing"
            )
        if target.status == ImportJobStatus.SCANNING and target.import_started_at is None:
            snapshot = dict(target.progress_snapshot or {})
            target.control_request = ImportControlRequest.PAUSE
            sync_progress_snapshot_state(
                target,
                status=ImportJobStatus.SCANNING,
                mode="scan",
                phase=str(snapshot.get("phase") or "scanning"),
                progress=int(snapshot.get("progress") or 0),
                message="Finishing the current scan checkpoint before pausing.",
            )
            return
        sync_paused_job_state(target)

    _apply_pause(job)
    job = await _flush_job_with_sqlite_lock_retry(
        session,
        job,
        job_id=job_id,
        mutate=_apply_pause,
    )
    pause_requested = job.control_request == ImportControlRequest.PAUSE
    await log_event(
        session,
        job_id,
        "INFO",
        "import_pause_requested" if pause_requested else "import_paused",
        message=(
            "Scan pause requested. Waiting for the next safe checkpoint."
            if pause_requested
            else paused_message_for_mode(snapshot_mode_for_job(job))
        ),
    )
    return job


async def resume_job(
    session: AsyncSession,
    job_id: int,
    *,
    log_event: ImportEventLogger,
) -> ImportJob:
    """Resume a paused or stalled import from its last safe checkpoint."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    if job.status not in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}:
        raise ValidationError(f"Cannot resume job in {job.status} state")

    phase = str((job.progress_snapshot or {}).get("phase") or "")
    if (
        job.status is ImportJobStatus.STALLED
        and job.import_started_at is not None
        and phase == "story_arc_placements"
    ):
        raise ValidationError(
            "Retry the failed Story Arc placement work or cancel the import; "
            "the completed canonical import must not be replayed."
        )
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
    elif phase in {"matching"}:
        job.status = ImportJobStatus.MATCHING
    elif phase in {"analyzing"}:
        job.status = ImportJobStatus.ANALYZING
    elif phase in {"file_matching"}:
        job.status = ImportJobStatus.FILE_MATCHING
    else:
        job.status = ImportJobStatus.SCANNING

    job.control_request = ImportControlRequest.NONE
    job.error_message = None
    sync_progress_snapshot_state(
        job,
        mode=mode,
        phase=phase or "scanning",
        message="Import resume requested.",
    )
    await session.flush()
    await log_event(
        session,
        job_id,
        "INFO",
        "import_resume_requested",
        message="Import resume requested.",
        resume_mode=mode,
        resume_phase=phase or "scanning",
    )
    return job


async def request_cancel(
    session: AsyncSession,
    job_id: int,
    *,
    log_event: ImportEventLogger,
) -> ImportJob:
    """Request cooperative cancellation or immediate discard for paused scans."""
    job = await session.get(
        ImportJob,
        job_id,
        populate_existing=True,
        with_for_update=True,
    )
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    if (
        job.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}
        and job.import_started_at is None
    ):
        await session.delete(job)
        await session.flush()
        return job

    def _apply_cancel(target: ImportJob) -> None:
        if _is_story_arc_placement_wait(target) or (
            target.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}
            and target.import_started_at is not None
        ):
            target.status = ImportJobStatus.ROLLING_BACK
            target.control_request = ImportControlRequest.CANCEL
        elif target.status in {
            ImportJobStatus.SCANNING,
            ImportJobStatus.ANALYZING,
            ImportJobStatus.MATCHING,
            ImportJobStatus.FILE_MATCHING,
            ImportJobStatus.IMPORTING,
        }:
            if target.import_started_at is not None:
                target.status = ImportJobStatus.CANCELLING
            target.control_request = ImportControlRequest.CANCEL
        else:
            raise ValidationError(f"Cannot cancel job in {target.status} state")
        target.error_message = "Import cancelled by user."
        if target.status == ImportJobStatus.ROLLING_BACK:
            target.story_arc_placement_followup_pending = False
            target.progress_snapshot = initialize_progress_snapshot(
                target,
                mode="rollback",
                phase="queued",
                progress=0,
                message="Cancelling import and rolling back changes...",
                status=ImportJobStatus.ROLLING_BACK,
            )
        else:
            sync_progress_snapshot_state(
                target,
                mode="import" if target.import_started_at is not None else "scan",
                phase=str((target.progress_snapshot or {}).get("phase") or "inventory"),
                message="Finishing the current safe step before cancelling.",
            )

    _apply_cancel(job)
    job = await _flush_job_with_sqlite_lock_retry(
        session,
        job,
        job_id=job_id,
        mutate=_apply_cancel,
    )
    await log_event(
        session,
        job_id,
        "INFO",
        "import_cancel_requested",
        message="Import cancelled by user.",
    )
    return job


async def request_rollback(
    session: AsyncSession,
    job_id: int,
    *,
    log_event: ImportEventLogger,
) -> ImportJob:
    """Queue a rollback of previously executed import actions."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.import_started_at is None:
        raise ValidationError("Rollback is only available after import execution has started")
    if job.status not in {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
    }:
        raise ValidationError(f"Cannot roll back job in {job.status} state")

    job.status = ImportJobStatus.ROLLING_BACK
    job.control_request = ImportControlRequest.NONE
    job.story_arc_placement_followup_pending = False
    job.progress_snapshot = initialize_progress_snapshot(
        job,
        mode="rollback",
        phase="queued",
        progress=0,
        message="Rolling back import actions...",
        status=ImportJobStatus.ROLLING_BACK,
    )
    await session.flush()
    await log_event(
        session,
        job_id,
        "INFO",
        "import_rollback_requested",
        message="Import rollback requested.",
    )
    return job
