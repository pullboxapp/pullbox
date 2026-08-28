"""Contracts for projecting utility jobs into shared activity progress."""

from __future__ import annotations

from pullbox.models.operation_progress import (
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)
from pullbox.services.utility_operation_progress import build_utility_operation_update
from pullbox.utilities.models import JobState, JobType, UtilityJob, UtilityJobItem


def _job(
    *,
    state: JobState = JobState.RUNNING,
    job_type: JobType = JobType.MASS_RENAME,
    created_by: str | None = "admin",
) -> UtilityJob:
    return UtilityJob(
        id="utility-1",
        job_type=job_type,
        display_name="Rename library files",
        state=state,
        config="{}",
        total_items=10,
        completed_items=4,
        failed_items=1,
        skipped_items=1,
        warning_count=0,
        created_by=created_by,
    )


def test_running_utility_job_projects_truthful_overall_and_current_item() -> None:
    item = UtilityJobItem(
        id="item-7",
        job_id="utility-1",
        item_index=7,
        state="IN_PROGRESS",
        file_path="/comics/Batman/Batman 007.cbz",
        operation="rename",
    )

    update = build_utility_operation_update(_job(), current_item=item)

    assert update.operation_type is OperationProgressType.UTILITY
    assert update.state is OperationProgressState.RUNNING
    assert update.visibility is OperationProgressVisibility.PROMINENT
    assert update.overall.current == 6
    assert update.overall.total == 10
    assert update.item is not None
    assert update.item.key == "item-7"
    assert update.item.label == "Batman 007.cbz"
    assert update.item.measure.percent is None


def test_failed_utility_job_requires_attention() -> None:
    job = _job(state=JobState.FAILED)
    job.error_message = "Rename could not be completed"

    update = build_utility_operation_update(job)

    assert update.state is OperationProgressState.FAILED
    assert update.tone is OperationProgressTone.DANGER
    assert update.attention_required is True
    assert update.message == "Rename could not be completed"


def test_completed_utility_job_projects_complete_progress() -> None:
    job = _job(state=JobState.COMPLETED)
    job.completed_items = 8

    update = build_utility_operation_update(job)

    assert update.state is OperationProgressState.COMPLETED
    assert update.tone is OperationProgressTone.SUCCESS
    assert update.overall.current == 10
    assert update.overall.percent == 100.0


def test_automatic_database_maintenance_is_quiet() -> None:
    update = build_utility_operation_update(
        _job(job_type=JobType.DB_CHECK_CLEANUP, created_by=None)
    )

    assert update.visibility is OperationProgressVisibility.QUIET
