"""Lifecycle/control response helpers for Import Jobs API routes."""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ImportJobCreate,
    ImportJobDeleteResponse,
    ImportJobRead,
    ImportPreviewResponse,
    RetryFailedResponse,
    RetryImportResponse,
    RetryStoryArcPlacementsResponse,
)
from pullbox.services.import_managed_copy_preflight import ManagedCopyPreflightError
from pullbox.services.story_arc_sync_queue import (
    discard_unpublished_import_story_arc_sync_work,
)

logger = structlog.get_logger(__name__)

_ROLLBACK_PENDING_DELETE_MESSAGE = (
    "Rollback is still stopping an in-progress Story Arc placement. "
    "The import remains in history; delete it again after rollback finishes."
)
_ROLLBACK_INCOMPLETE_DELETE_MESSAGE = (
    "Rollback is incomplete and some data requires manual recovery. "
    "The import remains in history so its recovery evidence is preserved."
)

_CLEARABLE_HISTORY_STATUSES = (
    ImportJobStatus.COMPLETED,
    ImportJobStatus.FAILED,
    ImportJobStatus.CANCELLED,
    ImportJobStatus.ROLLED_BACK,
)
_RESUMABLE_IMPORT_STATUSES = {
    ImportJobStatus.SCANNING,
    ImportJobStatus.ANALYZING,
    ImportJobStatus.MATCHING,
    ImportJobStatus.FILE_MATCHING,
    ImportJobStatus.IMPORTING,
    ImportJobStatus.ROLLING_BACK,
}


async def create_import_job_response(
    service: Any,
    *,
    session: Any,
    body: ImportJobCreate,
    trigger_import_scan: Any,
) -> ImportJobRead:
    """Create a new import job and trigger the scan task."""
    try:
        job = await service.create_job(session, body)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()

    logger.info("import_job_created", job_id=job.id, source_path=body.source_path)
    trigger_import_scan(job.id)

    return ImportJobRead.model_validate(job)


async def get_import_job_response(session: Any, job_id: int) -> ImportJobRead:
    """Return import job details or raise the standard not-found error."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    return ImportJobRead.model_validate(job)


async def get_import_preview_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    status: str | None,
    page: int,
    page_size: int,
) -> ImportPreviewResponse:
    """Return a paginated series preview for the review step."""
    status_filter: list[ImportSeriesStatus] | None = None
    if status:
        with contextlib.suppress(ValueError):
            status_filter = [ImportSeriesStatus(status)]

    result: ImportPreviewResponse = await service.get_preview(
        session,
        job_id,
        status_filter=status_filter,
        page=page,
        page_size=page_size,
    )
    return result


async def confirm_import_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    body: ConfirmImportRequest,
    trigger_import_execute: Any,
) -> ImportJobRead:
    """Confirm selected series for import and trigger the execute task."""
    try:
        job = await service.confirm_import(session, job_id, body)
    except ManagedCopyPreflightError as exc:
        # The service has restored REVIEW state and attached a sanitized live
        # capacity snapshot. Persist that evidence before returning the block.
        await session.commit()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()

    logger.info("import_confirmed", job_id=job_id)
    trigger_import_execute(job_id)

    return ImportJobRead.model_validate(job)


async def clear_import_history_response(session: Any) -> dict[str, int]:
    """Delete terminal import history records while leaving active jobs intact."""
    jobs_result = await session.execute(
        sa_select(ImportJob).where(
            ImportJob.status.in_(_CLEARABLE_HISTORY_STATUSES),
            ImportJob.archived_at.is_(None),
        )
    )
    jobs = list(jobs_result.scalars().all())

    try:
        await discard_unpublished_import_story_arc_sync_work(
            session,
            tuple(int(job.id) for job in jobs),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    for job in jobs:
        await session.delete(job)

    logger.info("import_history_cleared", count=len(jobs))
    return {"deleted": len(jobs)}


async def cancel_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    purge_import_runtime_state: Any,
) -> ImportJobDeleteResponse | None:
    """Cancel an active import job or delete a finished one from history."""
    try:
        action = await service.cancel_job(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    if action == "deleted":
        purge_import_runtime_state(job_id)
        logger.info("import_job_deleted", job_id=job_id)
        return None
    if action == "rollback_incomplete":
        raise HTTPException(status_code=409, detail=_ROLLBACK_INCOMPLETE_DELETE_MESSAGE)
    if action != "rollback_pending":
        raise RuntimeError(f"Unsupported import deletion result: {action!r}")
    logger.info("import_job_delete_waiting_for_story_arc_rollback", job_id=job_id)
    return ImportJobDeleteResponse(message=_ROLLBACK_PENDING_DELETE_MESSAGE)


async def pause_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
) -> ImportJobRead:
    """Request a cooperative pause for a running import job."""
    try:
        job = await service.pause_job(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    return ImportJobRead.model_validate(job)


async def resume_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_import_resume: Any,
) -> ImportJobRead:
    """Resume a paused import job and retrigger background work when needed."""
    try:
        job = await service.resume_job(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    if job.status in _RESUMABLE_IMPORT_STATUSES:
        trigger_import_resume(job_id)
    return ImportJobRead.model_validate(job)


async def request_cancel_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_import_rollback: Any,
    purge_import_runtime_state: Any,
) -> ImportJobRead | None:
    """Request cancellation of a running import job."""
    try:
        job = await service.request_cancel(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    if job.status == ImportJobStatus.ROLLING_BACK:
        trigger_import_rollback(job_id)
    if job.id and await session.get(ImportJob, job_id) is not None:
        return ImportJobRead.model_validate(job)
    purge_import_runtime_state(job_id)
    return None


async def rollback_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_import_rollback: Any,
) -> ImportJobRead:
    """Start rollback for a previously executed import job."""
    try:
        job = await service.request_rollback(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    trigger_import_rollback(job_id)
    return ImportJobRead.model_validate(job)


async def retry_failed_series_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_import_execute: Any,
) -> RetryFailedResponse:
    """Reset failed import work and re-trigger the import task."""
    try:
        job, count = await service.retry_failed_series(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()

    logger.info("import_retry_failed", job_id=job_id, retrying_count=count)
    if count > 0:
        trigger_import_execute(job_id)

    return RetryFailedResponse(job_id=job.id, retrying_count=count)


async def retry_story_arc_placements_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_story_arc_sync: Any,
) -> RetryStoryArcPlacementsResponse:
    """Requeue terminal import placement work and nudge its durable worker."""
    try:
        job, count = await service.retry_story_arc_placements(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    logger.info(
        "import_story_arc_placements_retry_requested",
        job_id=job_id,
        retrying_count=count,
    )
    trigger_story_arc_sync()
    return RetryStoryArcPlacementsResponse(job_id=job.id, retrying_count=count)


async def allow_safety_blocked_file_once_and_retry_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    file_id: int,
    trigger_import_execute: Any,
) -> RetryFailedResponse:
    """Approve one resource safety exception and re-trigger Step 4 for that file."""
    try:
        job, _imported_series = await service.allow_safety_blocked_file_once_for_retry(
            session,
            job_id,
            file_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    logger.info("import_safety_retry_started", job_id=job_id, file_id=file_id)
    trigger_import_execute(job_id)
    return RetryFailedResponse(job_id=job.id, retrying_count=1)


async def retry_import_job_response(
    service: Any,
    *,
    session: Any,
    job_id: int,
    trigger_import_scan: Any,
) -> RetryImportResponse:
    """Create a brand-new import job from a cancelled or rolled-back history row."""
    try:
        job = await service.retry_job(session, job_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    trigger_import_scan(job.id)
    return RetryImportResponse(
        job_id=job.id,
        redirect_url=f"/import?tab=collection&resume_job_id={job.id}&resume_step=2",
    )
