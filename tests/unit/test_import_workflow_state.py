"""Characterization tests for import workflow state helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_service import ImportService, import_control_state_for_job
from pullbox.services.import_workflow_state import (
    emit_live_progress,
    emit_progress,
    inventory_progress_hint,
    raise_if_job_cancelled,
    sync_paused_job_state,
)


def _job(
    status: ImportJobStatus,
    *,
    import_started: bool = False,
    transfer_method: str = "move",
    convert: bool = False,
) -> ImportJob:
    job = ImportJob(
        source_path="/library/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
    )
    job.transfer_method = transfer_method
    job.convert_to_preferred_format = convert
    if import_started:
        job.import_started_at = datetime.now(UTC) - timedelta(minutes=5)
    return job


def test_import_control_state_allows_active_scan_controls() -> None:
    state = import_control_state_for_job(
        _job(
            ImportJobStatus.SCANNING,
            transfer_method="copy",
            convert=True,
        )
    )

    assert state == {
        "can_pause": True,
        "can_resume": False,
        "can_cancel": True,
        "can_discard": False,
        "can_delete": False,
        "can_view_results": False,
        "can_retry": False,
        "can_retry_story_arc_placements": False,
        "can_rollback": False,
        "transfer_method": "copy",
        "convert_to_preferred_format": True,
        "requested_action": ImportControlRequest.NONE.value,
    }


def test_import_control_state_allows_paused_resume_discard_and_rollback() -> None:
    state = import_control_state_for_job(_job(ImportJobStatus.PAUSED, import_started=True))

    assert state["can_pause"] is False
    assert state["can_resume"] is True
    assert state["can_cancel"] is True
    assert state["can_discard"] is True
    assert state["can_delete"] is True
    assert state["can_retry"] is False
    assert state["can_view_results"] is False
    assert state["can_rollback"] is False


def test_import_control_state_does_not_offer_resume_for_stalled_placement_wait() -> None:
    job = _job(ImportJobStatus.STALLED, import_started=True)
    job.progress_snapshot = {
        "mode": "import",
        "phase": "story_arc_placements",
        "progress": 99,
        "story_arc_placements_failed": 1,
        "story_arc_placements_cancelled": 0,
    }

    state = import_control_state_for_job(job)

    assert state["can_resume"] is False
    assert state["can_cancel"] is True
    assert state["can_retry_story_arc_placements"] is True
    assert state["requested_action"] == ImportControlRequest.NONE.value


@pytest.mark.parametrize(
    ("failed", "cancelled"),
    [
        (0, 0),
        (True, 0),
        (0, True),
        ("1", 0),
    ],
)
def test_import_control_state_does_not_offer_placement_retry_without_terminal_counts(
    failed: object,
    cancelled: object,
) -> None:
    job = _job(ImportJobStatus.STALLED, import_started=True)
    job.progress_snapshot = {
        "mode": "import",
        "phase": "story_arc_placements",
        "progress": 99,
        "story_arc_placements_failed": failed,
        "story_arc_placements_cancelled": cancelled,
    }

    assert import_control_state_for_job(job)["can_retry_story_arc_placements"] is False


def test_import_control_state_allows_review_resume_before_import_starts() -> None:
    state = import_control_state_for_job(_job(ImportJobStatus.REVIEW))

    assert state["can_pause"] is False
    assert state["can_resume"] is True
    assert state["can_cancel"] is False
    assert state["can_delete"] is True
    assert state["can_view_results"] is False
    assert state["can_retry"] is False
    assert state["can_rollback"] is False
    assert state["requested_action"] == ImportControlRequest.NONE.value


def test_import_control_state_hides_cancel_during_active_rollback() -> None:
    state = import_control_state_for_job(_job(ImportJobStatus.ROLLING_BACK, import_started=True))

    assert state["can_pause"] is False
    assert state["can_resume"] is False
    assert state["can_cancel"] is False
    assert state["can_rollback"] is False
    assert state["requested_action"] == ImportControlRequest.NONE.value


def test_import_control_state_disallows_pause_during_story_arc_placement_wait() -> None:
    job = _job(ImportJobStatus.IMPORTING, import_started=True)
    job.progress_snapshot = {
        "mode": "import",
        "phase": "story_arc_placements",
        "progress": 99,
    }

    state = import_control_state_for_job(job)

    assert state["can_pause"] is False
    assert state["can_cancel"] is True
    assert state["can_retry_story_arc_placements"] is False


def test_import_control_state_does_not_offer_placement_retry_outside_exact_stall() -> None:
    wrong_phase = _job(ImportJobStatus.STALLED, import_started=True)
    wrong_phase.progress_snapshot = {
        "mode": "import",
        "phase": "importing",
        "progress": 83,
    }
    cancelling = _job(ImportJobStatus.STALLED, import_started=True)
    cancelling.progress_snapshot = {
        "mode": "import",
        "phase": "story_arc_placements",
        "progress": 99,
    }
    cancelling.control_request = ImportControlRequest.CANCEL

    assert import_control_state_for_job(wrong_phase)["can_retry_story_arc_placements"] is False
    assert import_control_state_for_job(cancelling)["can_retry_story_arc_placements"] is False


def test_import_phase_progress_scales_into_bounded_range() -> None:
    assert ImportService._phase_progress(35, 80, completed=5, total=10) == 57
    assert ImportService._phase_progress(35, 80, completed=99, total=10) == 80
    assert ImportService._phase_progress(35, 80, completed=-5, total=10) == 35
    assert ImportService._phase_progress(80, 35, completed=5, total=10) == 80
    assert ImportService._phase_progress(35, 80, completed=5, total=0) == 35


def test_inventory_progress_hint_advances_through_real_work_milestones() -> None:
    assert inventory_progress_hint(0) == 0
    assert inventory_progress_hint(1) == 1
    assert inventory_progress_hint(50) == 3
    assert inventory_progress_hint(5_000) == 6
    assert inventory_progress_hint(999_999) == 9


def test_import_estimate_remaining_seconds_uses_elapsed_progress() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=20)

    estimate = ImportService._estimate_remaining_seconds(started_at, progress=25)

    assert estimate is not None
    assert 55 <= estimate <= 65


def test_import_estimate_remaining_seconds_omits_unstable_values() -> None:
    assert ImportService._estimate_remaining_seconds(None, progress=25) is None
    assert ImportService._estimate_remaining_seconds(datetime.now(UTC), progress=25) is None
    assert ImportService._estimate_remaining_seconds(datetime.now(UTC), progress=0) is None
    assert ImportService._estimate_remaining_seconds(datetime.now(UTC), progress=100) is None


@pytest.mark.asyncio
async def test_emit_progress_persists_snapshot_and_invokes_callback(db_session) -> None:
    job = _job(ImportJobStatus.SCANNING, transfer_method="copy", convert=True)
    db_session.add(job)
    await db_session.flush()
    event = ImportProgressEvent(
        job_id=job.id,
        status=ImportJobStatus.SCANNING,
        phase="scanning",
        progress=25,
        message="Scanning...",
    )
    captured: list[ImportProgressEvent] = []

    async def capture_progress(progress_event: ImportProgressEvent) -> None:
        captured.append(progress_event)

    await emit_progress(db_session, job, event, progress_callback=capture_progress)

    assert captured == [event]
    assert event.control_state is not None
    assert event.control_state["can_pause"] is True
    assert job.progress_snapshot is not None
    assert job.progress_snapshot["progress"] == 25
    assert job.progress_snapshot["message"] == "Scanning..."
    assert job.progress_snapshot["control_state"]["transfer_method"] == "copy"


@pytest.mark.asyncio
async def test_emit_live_progress_advances_runtime_revision_without_snapshot_write(
    db_session,
) -> None:
    job = _job(ImportJobStatus.SCANNING)
    job.progress_revision = 7
    job.progress_snapshot = {
        "status": ImportJobStatus.SCANNING.value,
        "phase": "scanning",
        "progress": 10,
        "message": "Durable checkpoint",
        "progress_revision": 7,
    }
    db_session.add(job)
    await db_session.flush()
    revision_state = {"value": 7}
    event = ImportProgressEvent(
        job_id=job.id,
        status=ImportJobStatus.SCANNING,
        phase="scanning",
        progress=12,
        message="Inventory heartbeat",
        current_item_kind="scan",
        current_item_stage="inventory",
        current_item_progress_pct=12,
    )
    captured: list[ImportProgressEvent] = []

    async def capture_progress(progress_event: ImportProgressEvent) -> None:
        captured.append(progress_event)

    await emit_live_progress(
        job,
        event,
        progress_callback=capture_progress,
        revision_state=revision_state,
        started_at=job.scan_started_at,
    )

    assert len(captured) == 1
    assert captured[0].ephemeral_progress is True
    assert captured[0].progress_revision == 8
    assert captured[0].current_item_kind == "scan"
    assert revision_state["value"] == 8
    assert job.progress_snapshot["message"] == "Durable checkpoint"


@pytest.mark.asyncio
async def test_emit_progress_ignores_stale_active_event_after_pause(db_session) -> None:
    job = _job(ImportJobStatus.PAUSED, import_started=True)
    job.progress_snapshot = {
        "status": ImportJobStatus.PAUSED.value,
        "mode": "import",
        "phase": "importing",
        "progress": 64,
        "message": "Import is paused.",
        "current_file_name": "Fearscape Vol 02.pdf",
        "current_file_stage": "rendering",
        "current_file_progress_pct": 42,
        "control_state": {
            "can_pause": False,
            "can_resume": True,
            "can_cancel": True,
            "requested_action": ImportControlRequest.NONE.value,
        },
    }
    db_session.add(job)
    await db_session.flush()
    event = ImportProgressEvent(
        job_id=job.id,
        status=ImportJobStatus.IMPORTING,
        mode="import",
        phase="importing",
        progress=71,
        message="Processing file 3/5",
    )
    captured: list[ImportProgressEvent] = []

    async def capture_progress(progress_event: ImportProgressEvent) -> None:
        captured.append(progress_event)

    await emit_progress(db_session, job, event, progress_callback=capture_progress)
    await db_session.refresh(job)

    assert captured == []
    assert job.progress_snapshot["status"] == ImportJobStatus.PAUSED.value
    assert job.progress_snapshot["progress"] == 64
    assert job.progress_snapshot["message"] == "Import is paused."


@pytest.mark.asyncio
async def test_raise_if_job_cancelled_allows_active_status(db_session) -> None:
    job = _job(ImportJobStatus.MATCHING)
    db_session.add(job)
    await db_session.flush()

    await raise_if_job_cancelled(db_session, job.id)


@pytest.mark.asyncio
async def test_raise_if_job_cancelled_raises_for_cancelled_status(db_session) -> None:
    job = _job(ImportJobStatus.CANCELLING)
    db_session.add(job)
    await db_session.flush()

    with pytest.raises(JobCancelledError, match=f"Import job {job.id} was cancelled"):
        await raise_if_job_cancelled(db_session, job.id)


@pytest.mark.asyncio
async def test_raise_if_job_cancelled_raises_for_missing_job(db_session) -> None:
    with pytest.raises(JobCancelledError, match="Import job 9999 was cancelled"):
        await raise_if_job_cancelled(db_session, 9999)


@pytest.mark.asyncio
async def test_raise_if_job_cancelled_raises_for_paused_status(db_session) -> None:
    job = _job(ImportJobStatus.PAUSING)
    db_session.add(job)
    await db_session.flush()

    with pytest.raises(JobPausedError, match=f"Import job {job.id} was paused"):
        await raise_if_job_cancelled(db_session, job.id)


def test_sync_paused_job_state_uses_error_message_when_present() -> None:
    job = _job(ImportJobStatus.MATCHING)
    job.error_message = "ComicVine timed out while matching 6 series."
    job.progress_snapshot = {
        "mode": "scan",
        "phase": "matching",
        "progress": 54,
        "message": "Matching against ComicVine...",
    }

    sync_paused_job_state(job)

    assert job.status == ImportJobStatus.PAUSED
    assert job.progress_snapshot["message"] == "ComicVine timed out while matching 6 series."
