"""Tests for mapping import workflow events into shared operation progress."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressTone,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_operation_progress import build_import_operation_update
from pullbox.services.import_workflow_state import persist_progress_snapshot


def _job(*, status: ImportJobStatus = ImportJobStatus.FILE_MATCHING) -> ImportJob:
    return ImportJob(
        id=42,
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=status,
        scan_started_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        series_failed=0,
    )


def _event(*, revision: int = 8) -> ImportProgressEvent:
    return ImportProgressEvent(
        job_id=42,
        status=ImportJobStatus.FILE_MATCHING,
        mode="scan",
        phase="file_matching",
        progress=84,
        progress_revision=revision,
        message="Matching issue files",
        current_series_id=9,
        current_series_name="Batman",
        current_file_id=21,
        current_file_name="Batman 001.cbz",
        current_item_kind="file",
        current_item_stage="file_matching",
        current_item_stage_label="Matching files to issues",
        current_item_progress_pct=55,
        estimated_seconds_remaining=120,
    )


def test_import_event_maps_to_complete_shared_progress_update() -> None:
    update = build_import_operation_update(_job(), _event())

    assert update.operation_key == "42"
    assert update.state is OperationProgressState.RUNNING
    assert update.title == "Mylar import"
    assert update.source_label == "Mylar import"
    assert update.overall.percent == 84
    assert update.item is not None
    assert update.item.key == "file:21"
    assert update.item.label == "Batman 001.cbz"
    assert update.item.measure.percent == 55
    assert update.eta_seconds == 120
    assert update.detail_url == "/import?tab=collection&resume_job_id=42&resume_step=2"


def test_failed_import_is_attention_requiring_danger_state() -> None:
    job = _job(status=ImportJobStatus.FAILED)
    event = _event()
    event.status = ImportJobStatus.FAILED
    event.phase = "failed"

    update = build_import_operation_update(job, event)

    assert update.state is OperationProgressState.FAILED
    assert update.tone is OperationProgressTone.DANGER
    assert update.attention_required is True


async def test_persist_import_snapshot_dual_writes_shared_operation(db_session) -> None:
    job = _job()
    db_session.add(job)
    await db_session.flush()

    await persist_progress_snapshot(db_session, job, _event())

    operation = (
        await db_session.execute(
            select(OperationProgress).where(
                OperationProgress.operation_key == "42",
            )
        )
    ).scalar_one()
    assert operation.revision == 8
    assert operation.overall_percent == 84
    assert operation.item_percent == 55
    assert operation.item_label == "Batman 001.cbz"
