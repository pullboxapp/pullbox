"""Eligibility boundaries for post-import recovery actions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.import_terminal_recovery import allows_terminal_import_recovery


def _job(status: ImportJobStatus) -> ImportJob:
    return ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=status,
        control_request=ImportControlRequest.NONE,
        progress_snapshot={},
    )


def test_completed_and_durably_completed_failed_jobs_allow_recovery() -> None:
    completed = _job(ImportJobStatus.COMPLETED)
    failed_after_completion = _job(ImportJobStatus.FAILED)
    failed_after_completion.import_completed_at = datetime.now(UTC)

    assert allows_terminal_import_recovery(completed) is True
    assert allows_terminal_import_recovery(failed_after_completion) is True


@pytest.mark.parametrize("guard", ["no_completion", "archived", "control", "rollback"])
def test_incomplete_or_mutating_terminal_jobs_reject_recovery(guard: str) -> None:
    job = _job(ImportJobStatus.FAILED)
    job.import_completed_at = datetime.now(UTC)
    if guard == "no_completion":
        job.import_completed_at = None
    elif guard == "archived":
        job.archived_at = datetime.now(UTC)
    elif guard == "control":
        job.control_request = ImportControlRequest.CANCEL
    else:
        job.progress_snapshot = {"mode": "rollback"}

    assert allows_terminal_import_recovery(job) is False
