"""Mylar scan ETA uses completed source batches, not individual files."""

from datetime import UTC, datetime, timedelta

import pytest

from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.services import import_progress_runtime
from pullbox.services.import_mylar_scan_progress import MylarScanProgress
from pullbox.services.import_operation_progress import build_import_operation_update
from pullbox.ui.import_progress_snapshot import build_import_progress_snapshot


@pytest.fixture
def scan_clock(monkeypatch):
    clock = {"seconds": 0, "starts": []}

    def elapsed_seconds(started_at):
        clock["starts"].append(started_at)
        return clock["seconds"] if started_at is not None else None

    monkeypatch.setattr(import_progress_runtime, "elapsed_seconds_since", elapsed_seconds)
    return clock


@pytest.fixture
async def scan_job(db_session):
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        scan_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(job)
    await db_session.commit()
    return job


async def no_cancellation():
    pass


@pytest.mark.parametrize("with_callback", [True, False])
async def test_scan_eta_reaches_live_updates_and_durable_snapshot(
    db_session, scan_job, scan_clock, with_callback
):
    events = []

    async def capture(event):
        assert scan_job.progress_snapshot["estimated_seconds_remaining"] == (
            event.estimated_seconds_remaining
        )
        events.append(event)

    progress = MylarScanProgress(
        db_session, scan_job, 1000, no_cancellation, callback=capture if with_callback else None
    )
    progress.source_page_end = 100
    # Annual cohorts may outnumber the original Mylar source rows.
    scan_job.series_found = 350
    scan_clock["seconds"] = 10
    await progress.checkpoint_page()
    await db_session.refresh(scan_job)

    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 90
    hydrated = build_import_progress_snapshot(
        scan_job, review_summary={}, recent_logs=[], progress_revision=scan_job.progress_revision
    )
    assert hydrated["estimated_seconds_remaining"] == 90
    if with_callback:
        assert events[-1].estimated_seconds_remaining == 90
        assert build_import_operation_update(scan_job, events[-1]).eta_seconds == 90


async def test_scan_eta_waits_for_saved_batch_and_does_not_reset_with_each_file(
    db_session, scan_job, scan_clock
):
    progress = MylarScanProgress(db_session, scan_job, 1000, no_cancellation)
    progress.source_page_end = 100
    await progress.report_safety(0, 400, "")
    scan_clock["seconds"] = 10
    await progress.report_safety(400, 400, "/comics/issue.cbz")
    assert scan_job.progress_snapshot["current_item_progress_pct"] == 100
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] is None

    scan_clock["seconds"] = 20
    await progress.checkpoint_page()
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 180
    progress.source_page_end = 200
    scan_clock["seconds"] = 30
    await progress.report_safety(0, 200, "")
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 180
    await progress.report_safety(200, 200, "/comics/another.cbz")
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 180

    scan_clock["seconds"] = 40
    await progress.checkpoint_page()
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 160


async def test_scan_eta_uses_shared_warmup_and_current_scan_clock(db_session, scan_job, scan_clock):
    invocation_start = datetime.now(UTC)
    progress = MylarScanProgress(db_session, scan_job, 1000, no_cancellation)
    progress.source_page_end = 100
    scan_clock["seconds"] = 1
    await progress.checkpoint_page()
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] is None

    progress.source_page_end = 200
    scan_clock["seconds"] = 2
    await progress.checkpoint_page()
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == 8
    # A resumed scan must not count yesterday's scan/pause as current throughput.
    assert all(start >= invocation_start for start in scan_clock["starts"])


@pytest.mark.parametrize("source_total", [0, 100])
async def test_scan_eta_handles_unknown_total_and_finished_phase(
    db_session, scan_job, scan_clock, source_total
):
    progress = MylarScanProgress(db_session, scan_job, source_total, no_cancellation)
    progress.source_page_end = 100
    scan_clock["seconds"] = 10
    await progress.checkpoint_page()
    assert scan_job.progress_snapshot["estimated_seconds_remaining"] == (
        0 if source_total else None
    )
    assert scan_job.status == ImportJobStatus.SCANNING
    if source_total:
        assert scan_job.progress_snapshot["progress"] == 35
