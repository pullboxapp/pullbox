"""Shared eligibility rules for safe actions after a durable import completion."""

from __future__ import annotations

from pullbox.models.import_job import ImportControlRequest, ImportJob, ImportJobStatus


def allows_terminal_import_recovery(job: ImportJob) -> bool:
    """Return whether post-import actions may safely operate on this job."""
    if (
        job.archived_at is not None
        or job.control_request is not ImportControlRequest.NONE
        or job.story_arc_rollback_waiting_work_id is not None
        or dict(job.progress_snapshot or {}).get("mode") == "rollback"
    ):
        return False
    if job.status is ImportJobStatus.COMPLETED:
        return True
    return job.status is ImportJobStatus.FAILED and job.import_completed_at is not None
