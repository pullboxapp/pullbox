"""Adapter from import workflow events to shared operation progress."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.models.import_job import ImportJobStatus, ImportSourceType
from pullbox.models.operation_progress import (
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)
from pullbox.services.operation_progress import (
    OperationItemProgress,
    OperationProgressMeasure,
    OperationProgressUpdate,
)

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportJob
    from pullbox.schemas.import_job import ImportProgressEvent


_RUNNING_STATUSES = frozenset(
    {
        ImportJobStatus.SCANNING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }
)


def _operation_state(status: ImportJobStatus) -> OperationProgressState:
    if status is ImportJobStatus.PENDING:
        return OperationProgressState.QUEUED
    if status in _RUNNING_STATUSES:
        return OperationProgressState.RUNNING
    if status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}:
        return OperationProgressState.PAUSED
    if status in {
        ImportJobStatus.REVIEW,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.ROLLED_BACK,
    }:
        return OperationProgressState.COMPLETED
    if status is ImportJobStatus.CANCELLED:
        return OperationProgressState.CANCELLED
    return OperationProgressState.FAILED


def _operation_tone(
    status: ImportJobStatus,
    *,
    has_failures: bool,
) -> OperationProgressTone:
    if status is ImportJobStatus.FAILED:
        return OperationProgressTone.DANGER
    if (
        status
        in {
            ImportJobStatus.PAUSED,
            ImportJobStatus.STALLED,
            ImportJobStatus.CANCELLED,
        }
        or has_failures
    ):
        return OperationProgressTone.WARNING
    if status in {
        ImportJobStatus.REVIEW,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.ROLLED_BACK,
    }:
        return OperationProgressTone.SUCCESS
    return OperationProgressTone.INFO


def _detail_url(job_id: int, event: ImportProgressEvent) -> str:
    if event.status is ImportJobStatus.ROLLED_BACK:
        return "/import?tab=history"
    if event.status is ImportJobStatus.REVIEW:
        step = 3
    elif event.status is ImportJobStatus.COMPLETED:
        step = 5
    elif event.mode == "import":
        step = 4
    else:
        step = 2
    return f"/import?tab=collection&resume_job_id={job_id}&resume_step={step}"


def _current_item(event: ImportProgressEvent) -> OperationItemProgress | None:
    item_stage = event.current_item_stage or event.current_file_stage or event.phase
    item_message = event.current_item_stage_label or event.current_item_detail or event.message
    if event.current_file_id is not None or event.current_file_name:
        key = (
            f"file:{event.current_file_id}"
            if event.current_file_id is not None
            else f"file-name:{event.current_file_name}"
        )
        return OperationItemProgress(
            key=key,
            label=event.current_file_name or "Current file",
            phase=item_stage,
            message=item_message,
            measure=OperationProgressMeasure(
                current=event.current_file_progress_current,
                total=event.current_file_progress_total,
                percent=event.current_item_progress_pct,
                unit=event.current_file_progress_unit,
            ),
        )
    series_name = event.current_series_name or event.current_series
    if event.current_series_id is None and not series_name:
        return None
    key = (
        f"series:{event.current_series_id}"
        if event.current_series_id is not None
        else f"series-name:{series_name}"
    )
    return OperationItemProgress(
        key=key,
        label=series_name or "Current series",
        phase=item_stage,
        message=item_message,
        measure=OperationProgressMeasure(
            percent=event.current_item_progress_pct,
            unit="percent",
        ),
    )


def build_import_operation_update(
    job: ImportJob,
    event: ImportProgressEvent,
) -> OperationProgressUpdate:
    """Map an import event into the shared durable projection contract."""
    source_label = "Mylar import" if job.source_type is ImportSourceType.MYLAR3 else "Folder import"
    has_failures = bool((event.series_failed or job.series_failed or 0) > 0)
    attention_required = event.status in {
        ImportJobStatus.FAILED,
        ImportJobStatus.PAUSED,
        ImportJobStatus.STALLED,
    } or (event.status is ImportJobStatus.COMPLETED and has_failures)
    started_at = job.import_started_at if event.mode == "import" else job.scan_started_at
    return OperationProgressUpdate(
        operation_type=OperationProgressType.IMPORT,
        operation_key=str(job.id),
        group_key=f"import:{job.id}",
        revision=event.progress_revision,
        state=_operation_state(event.status),
        phase=event.phase,
        title=source_label,
        message=event.message,
        source_label=source_label,
        detail_url=_detail_url(job.id, event),
        visibility=OperationProgressVisibility.PROMINENT,
        tone=_operation_tone(event.status, has_failures=has_failures),
        attention_required=attention_required,
        overall=OperationProgressMeasure(percent=event.progress, unit="percent"),
        item=_current_item(event),
        eta_seconds=event.estimated_seconds_remaining,
        detail_snapshot={
            "job_id": job.id,
            "mode": event.mode,
            "source_type": job.source_type.value,
            "series_imported": event.series_imported or job.series_imported or 0,
            "series_failed": event.series_failed or job.series_failed or 0,
            "files_imported": event.total_files_imported or 0,
            "files_failed": event.total_files_failed or 0,
        },
        started_at=started_at,
        event_at=event.last_checkpoint_at,
    )
