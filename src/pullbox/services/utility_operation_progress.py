"""Project utility queue jobs into the shared background activity contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    publish_operation_progress,
)
from pullbox.utilities.models import JobState, JobType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.utilities.models import UtilityJob, UtilityJobItem


def _shared_state(
    state: JobState,
) -> tuple[OperationProgressState, OperationProgressTone, bool]:
    if state in {JobState.COMPLETED, JobState.ROLLED_BACK}:
        return OperationProgressState.COMPLETED, OperationProgressTone.SUCCESS, False
    if state is JobState.FAILED:
        return OperationProgressState.FAILED, OperationProgressTone.DANGER, True
    if state is JobState.CANCELLED:
        return OperationProgressState.CANCELLED, OperationProgressTone.WARNING, False
    if state in {JobState.PAUSED, JobState.PAUSING}:
        return OperationProgressState.PAUSED, OperationProgressTone.WARNING, False
    if state is JobState.QUEUED:
        return OperationProgressState.QUEUED, OperationProgressTone.INFO, False
    if state is JobState.CANCELLING:
        return OperationProgressState.RUNNING, OperationProgressTone.WARNING, False
    return OperationProgressState.RUNNING, OperationProgressTone.INFO, False


def _display_filename(file_path: str | None) -> str | None:
    if not file_path:
        return None
    return file_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _current_item(item: UtilityJobItem | None) -> OperationItemProgress | None:
    if item is None:
        return None
    label = _display_filename(item.file_path) or f"Item {item.item_index + 1}"
    return OperationItemProgress(
        key=item.id,
        label=label,
        phase=item.operation,
        message="Processing",
        measure=OperationProgressMeasure(),
    )


def build_utility_operation_update(
    job: UtilityJob,
    *,
    current_item: UtilityJobItem | None = None,
) -> OperationProgressUpdate:
    """Build a shared progress snapshot from the utility queue's durable state."""
    job_state = JobState(job.state)
    state, tone, attention = _shared_state(job_state)
    total = job.total_items
    processed = (job.completed_items or 0) + (job.failed_items or 0) + (job.skipped_items or 0)
    if state is OperationProgressState.COMPLETED and total is not None:
        processed = total

    visibility = OperationProgressVisibility.PROMINENT
    if job.job_type == JobType.DB_CHECK_CLEANUP and job.created_by is None:
        visibility = OperationProgressVisibility.QUIET

    message = job.error_message or job_state.value.replace("_", " ").title()
    return OperationProgressUpdate(
        operation_type=OperationProgressType.UTILITY,
        operation_key=job.id,
        group_key="utilities",
        revision=None,
        state=state,
        phase=job_state.value.lower(),
        title=job.display_name,
        message=message,
        source_label="Utilities",
        detail_url="/utilities?tab=queue",
        visibility=visibility,
        tone=tone,
        attention_required=attention,
        overall=OperationProgressMeasure(
            current=processed,
            total=total,
            percent=100.0 if state is OperationProgressState.COMPLETED else None,
            unit="items",
        ),
        item=_current_item(current_item),
        detail_snapshot={
            "job_type": str(job.job_type),
            "failed_items": job.failed_items or 0,
            "skipped_items": job.skipped_items or 0,
            "warning_count": job.warning_count or 0,
        },
    )


async def project_utility_operation_progress(
    session: AsyncSession,
    job: UtilityJob,
    *,
    current_item: UtilityJobItem | None = None,
) -> None:
    """Persist the latest shared activity projection for a utility job."""
    await publish_operation_progress(
        session,
        build_utility_operation_update(job, current_item=current_item),
    )
