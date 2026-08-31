"""Tests for import job-control helper shims."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from pullbox.core.exceptions import JobPausedError, ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_service() -> ImportService:
    return ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )


async def _create_job_row(
    session: AsyncSession,
    *,
    status: ImportJobStatus,
    progress_snapshot: dict[str, object] | None = None,
    import_started_at: datetime | None = None,
) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        progress_snapshot=progress_snapshot,
        import_started_at=import_started_at,
    )
    session.add(job)
    await session.flush()
    return job


async def test_cancel_job_deletes_discardable_history_row(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, status=ImportJobStatus.COMPLETED)
    job_id = job.id

    result = await service.cancel_job(db_session, job_id)

    assert result == "deleted"
    assert await db_session.get(ImportJob, job_id) is None


async def test_cancel_job_deletes_stalled_scan_without_file_work(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, status=ImportJobStatus.STALLED)
    job_id = job.id

    result = await service.cancel_job(db_session, job_id)

    assert result == "deleted"
    assert await db_session.get(ImportJob, job_id) is None


async def test_cancel_job_rejects_active_job(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)

    with pytest.raises(ValidationError, match="pause/cancel/rollback"):
        await service.cancel_job(db_session, job.id)


async def test_pause_job_marks_job_paused_and_logs_event(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.MATCHING,
        progress_snapshot={
            "mode": "scan",
            "phase": "matching",
            "progress": 52,
            "message": "Matching live",
        },
    )

    updated = await service.pause_job(db_session, job.id)

    assert updated.status == ImportJobStatus.PAUSED
    assert updated.control_request == ImportControlRequest.NONE
    assert updated.progress_snapshot["status"] == ImportJobStatus.PAUSED.value
    assert updated.progress_snapshot["phase"] == "matching"
    assert updated.progress_snapshot["progress"] == 52
    assert updated.progress_snapshot["message"] == "Scan is paused."
    assert updated.progress_snapshot["requested_action"] == ImportControlRequest.NONE.value
    assert updated.progress_snapshot["control_state"]["can_resume"] is True
    assert updated.progress_snapshot["control_state"]["can_cancel"] is True
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_paused",
            )
        )
    ).scalar_one()
    assert log.message == "Scan is paused."


async def test_pause_job_rejects_story_arc_placement_wait(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.IMPORTING,
        import_started_at=datetime.now(UTC),
        progress_snapshot={
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
        },
    )

    with pytest.raises(ValidationError, match="cannot be paused"):
        await service.pause_job(db_session, job.id)

    assert job.status is ImportJobStatus.IMPORTING


async def test_cancel_story_arc_placement_wait_enters_rollback_immediately(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.IMPORTING,
        import_started_at=datetime.now(UTC),
        progress_snapshot={
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
        },
    )
    job.story_arc_placement_followup_pending = True

    updated = await service.request_cancel(db_session, job.id)

    assert updated.status is ImportJobStatus.ROLLING_BACK
    assert updated.control_request is ImportControlRequest.CANCEL
    assert updated.story_arc_placement_followup_pending is False
    assert updated.progress_snapshot["mode"] == "rollback"
    assert updated.progress_snapshot["phase"] == "queued"


async def test_pause_scanning_job_requests_safe_checkpoint(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.SCANNING,
        progress_snapshot={
            "mode": "scan",
            "phase": "scanning",
            "progress": 18,
            "message": "Scanning live",
        },
    )

    updated = await service.pause_job(db_session, job.id)

    assert updated.status == ImportJobStatus.SCANNING
    assert updated.control_request == ImportControlRequest.PAUSE
    assert updated.progress_snapshot["status"] == ImportJobStatus.SCANNING.value
    assert updated.progress_snapshot["phase"] == "scanning"
    assert updated.progress_snapshot["progress"] == 18
    assert (
        updated.progress_snapshot["message"]
        == "Finishing the current scan checkpoint before pausing."
    )
    assert updated.progress_snapshot["requested_action"] == ImportControlRequest.PAUSE.value
    assert updated.progress_snapshot["control_state"]["can_pause"] is False
    assert updated.progress_snapshot["control_state"]["can_resume"] is False
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_pause_requested",
            )
        )
    ).scalar_one()
    assert log.message == "Scan pause requested. Waiting for the next safe checkpoint."


async def test_pause_job_retries_transient_sqlite_lock(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "importing", "progress": 41, "message": "Importing"},
    )
    await db_session.commit()

    original_flush = db_session.flush
    flush_calls = 0

    async def flaky_flush(*args, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 1:
            raise OperationalError("UPDATE import_jobs", None, Exception("database is locked"))
        return await original_flush(*args, **kwargs)

    with (
        patch.object(db_session, "flush", side_effect=flaky_flush),
        patch("pullbox.services.import_job_controls.asyncio.sleep", new=AsyncMock()),
    ):
        updated = await service.pause_job(db_session, job.id)

    assert flush_calls == 2
    assert updated.status == ImportJobStatus.PAUSED
    assert updated.control_request == ImportControlRequest.NONE


async def test_raise_if_job_cancelled_immediately_uses_active_session_for_memory_db(
    db_session: AsyncSession,
) -> None:
    from pullbox.services.import_job_controls import raise_if_job_cancelled_immediately

    seen_sessions: list[object] = []

    async def fake_raise_if_job_cancelled(session_obj, _job_id):
        seen_sessions.append(session_obj)
        raise JobPausedError("paused")

    with pytest.raises(JobPausedError):
        await raise_if_job_cancelled_immediately(
            db_session,
            456,
            raise_if_cancelled=fake_raise_if_job_cancelled,
        )

    assert seen_sessions == [db_session]


async def test_resume_job_uses_progress_snapshot_phase(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.PAUSED,
        progress_snapshot={
            "mode": "import",
            "phase": "importing",
            "progress": 83,
            "message": "Paused",
        },
        import_started_at=datetime.now(UTC),
    )
    job.error_message = "ComicVine timed out while matching 6 series."

    updated = await service.resume_job(db_session, job.id)

    assert updated.status == ImportJobStatus.IMPORTING
    assert updated.error_message is None
    assert updated.progress_snapshot["status"] == ImportJobStatus.IMPORTING.value
    assert updated.progress_snapshot["phase"] == "importing"
    assert updated.progress_snapshot["message"] == "Import resume requested."


async def test_resume_stalled_job_uses_progress_snapshot_phase(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.STALLED,
        progress_snapshot={
            "mode": "scan",
            "phase": "file_matching",
            "progress": 87,
            "message": "Import stalled because the database was busy. Resume when ready.",
        },
    )
    job.error_message = "Import stalled because the database was busy. Resume when ready."

    updated = await service.resume_job(db_session, job.id)

    assert updated.status == ImportJobStatus.FILE_MATCHING
    assert updated.error_message is None
    assert updated.progress_snapshot["status"] == ImportJobStatus.FILE_MATCHING.value
    assert updated.progress_snapshot["phase"] == "file_matching"
    assert updated.progress_snapshot["message"] == "Import resume requested."


async def test_resume_rejects_stalled_story_arc_placement_wait(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.STALLED,
        progress_snapshot={
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
            "message": "One or more Story Arc placements failed.",
        },
        import_started_at=datetime.now(UTC),
    )

    with pytest.raises(
        ValidationError,
        match="Retry the failed Story Arc placement work or cancel the import",
    ):
        await service.resume_job(db_session, job.id)

    assert job.status is ImportJobStatus.STALLED
    assert job.progress_snapshot["phase"] == "story_arc_placements"


async def test_retry_story_arc_placements_delegates_and_logs_requeued_count(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.STALLED,
        progress_snapshot={
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
        },
        import_started_at=datetime.now(UTC),
    )
    retry = AsyncMock(return_value=(job, 2))

    with patch(
        "pullbox.services.import_service_job_lifecycle.retry_import_story_arc_placements",
        retry,
    ):
        updated, retrying_count = await service.retry_story_arc_placements(
            db_session,
            job.id,
        )

    assert updated is job
    assert retrying_count == 2
    retry.assert_awaited_once_with(db_session, job.id)
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_story_arc_placements_retry_requested",
            )
        )
    ).scalar_one()
    assert log.message == "Retrying 2 failed or cancelled Story Arc placements."
    assert log.data["retrying_count"] == 2


async def test_resume_job_rejects_rolled_back_history_row(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.ROLLED_BACK,
        progress_snapshot={"phase": "rollback"},
        import_started_at=datetime.now(UTC),
    )

    with pytest.raises(ValidationError, match="Cannot resume job in rolled_back state"):
        await service.resume_job(db_session, job.id)


async def test_request_cancel_sets_active_job_control_request(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.SCANNING,
        progress_snapshot={"phase": "scanning", "progress": 9, "message": "Scanning"},
    )

    updated = await service.request_cancel(db_session, job.id)

    assert updated.status == ImportJobStatus.SCANNING
    assert updated.control_request == ImportControlRequest.CANCEL
    assert updated.error_message == "Import cancelled by user."
    assert updated.progress_snapshot["status"] == ImportJobStatus.SCANNING.value
    assert updated.progress_snapshot["phase"] == "scanning"
    assert (
        updated.progress_snapshot["message"] == "Finishing the current safe step before cancelling."
    )
    assert updated.progress_snapshot["requested_action"] == ImportControlRequest.CANCEL.value


async def test_request_cancel_retries_transient_sqlite_lock(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "importing", "progress": 58, "message": "Importing"},
    )
    await db_session.commit()

    original_flush = db_session.flush
    flush_calls = 0

    async def flaky_flush(*args, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 1:
            raise OperationalError("UPDATE import_jobs", None, Exception("database is locked"))
        return await original_flush(*args, **kwargs)

    with (
        patch.object(db_session, "flush", side_effect=flaky_flush),
        patch("pullbox.services.import_job_controls.asyncio.sleep", new=AsyncMock()),
    ):
        updated = await service.request_cancel(db_session, job.id)

    assert flush_calls == 2
    assert updated.control_request == ImportControlRequest.CANCEL


async def test_request_cancel_paused_import_marks_cancelled_snapshot(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.PAUSED,
        progress_snapshot={"phase": "importing", "progress": 76, "message": "Paused"},
        import_started_at=datetime.now(UTC),
    )

    updated = await service.request_cancel(db_session, job.id)

    assert updated.status == ImportJobStatus.ROLLING_BACK
    assert updated.control_request == ImportControlRequest.CANCEL
    assert updated.progress_snapshot["status"] == ImportJobStatus.ROLLING_BACK.value
    assert updated.progress_snapshot["mode"] == "rollback"
    assert updated.progress_snapshot["phase"] == "queued"
    assert updated.progress_snapshot["progress"] == 0
    assert updated.progress_snapshot["message"] == "Cancelling import and rolling back changes..."


async def test_request_cancel_stalled_import_marks_rollback_snapshot(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.STALLED,
        progress_snapshot={"phase": "importing", "progress": 76, "message": "Stalled"},
        import_started_at=datetime.now(UTC),
    )

    updated = await service.request_cancel(db_session, job.id)

    assert updated.status == ImportJobStatus.ROLLING_BACK
    assert updated.control_request == ImportControlRequest.CANCEL
    assert updated.progress_snapshot["status"] == ImportJobStatus.ROLLING_BACK.value
    assert updated.progress_snapshot["mode"] == "rollback"
    assert updated.progress_snapshot["phase"] == "queued"
    assert updated.progress_snapshot["message"] == "Cancelling import and rolling back changes..."


async def test_request_rollback_requires_started_import(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, status=ImportJobStatus.COMPLETED)

    with pytest.raises(ValidationError, match="after import execution has started"):
        await service.request_rollback(db_session, job.id)


async def test_request_rollback_marks_job_and_logs_event(db_session: AsyncSession) -> None:
    service = _make_service()
    job = await _create_job_row(
        db_session,
        status=ImportJobStatus.COMPLETED,
        import_started_at=datetime.now(UTC),
    )
    job.story_arc_placement_followup_pending = True

    updated = await service.request_rollback(db_session, job.id)

    assert updated.status == ImportJobStatus.ROLLING_BACK
    assert updated.story_arc_placement_followup_pending is False
    assert updated.progress_snapshot["status"] == ImportJobStatus.ROLLING_BACK.value
    assert updated.progress_snapshot["phase"] == "queued"
    assert updated.progress_snapshot["message"] == "Rolling back import actions..."
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_rollback_requested",
            )
        )
    ).scalar_one()
    assert log.message == "Import rollback requested."
