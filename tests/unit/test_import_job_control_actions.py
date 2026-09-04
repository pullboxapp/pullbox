"""Direct tests for import job lifecycle API action helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from pullbox.api.v1.import_job_control_actions import (
    cancel_import_job_response,
    clear_import_history_response,
    confirm_import_response,
    get_import_preview_response,
    resume_import_job_response,
    retry_failed_series_response,
)
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import ConfirmImportRequest, ImportPreviewResponse
from pullbox.services.import_managed_copy_preflight import (
    ManagedCopyCapacitySnapshot,
    ManagedCopyPreflightError,
    ManagedCopyPreflightFailure,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _import_job(status: ImportJobStatus = ImportJobStatus.REVIEW) -> ImportJob:
    return ImportJob(
        id=42,
        source_path="/tmp/import",
        selected_file_paths=[],
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        scan_total_files=0,
        scan_total_dirs=0,
        series_found=0,
        series_duplicate=0,
        series_matched=0,
        series_no_match=0,
        series_new=0,
        series_imported=0,
        series_failed=0,
        monitored=False,
        search_on_add=False,
        move_to_library=True,
        transfer_method="move",
        convert_to_preferred_format=False,
        update_embedded_comicinfo_from_match=False,
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
        source_layout_snapshot={
            "schema_version": 1,
            "mode": "auto",
            "preset": None,
            "series_path_template": None,
            "issue_filename_template": None,
            "selected_cluster_id": None,
            "fallback_to_auto": True,
        },
        future_layout_requested=False,
        future_root_policy_snapshot=None,
        future_root_policy_applied_at=None,
        mylar3_path_map={},
        mylar3_path_map_confirmed=False,
        story_arc_import_requested=False,
        story_arc_materialization_requested=False,
        cv_match_threshold=0.70,
        min_files_per_series=1,
        progress_snapshot={},
        progress_revision=0,
        control_request=ImportControlRequest.NONE,
        total_files_duplicate=0,
        total_files_already_owned=0,
        created_at=datetime(2026, 6, 9, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_get_import_preview_response_filters_valid_status() -> None:
    """Valid status query strings should be translated before hitting the service."""
    service = AsyncMock()
    expected = MagicMock(spec=ImportPreviewResponse)
    service.get_preview.return_value = expected

    result = await get_import_preview_response(
        service,
        session=MagicMock(),
        job_id=7,
        status="matched",
        page=2,
        page_size=25,
    )

    assert result is expected
    service.get_preview.assert_awaited_once()
    _, _, kwargs = service.get_preview.mock_calls[0]
    assert [status.value for status in kwargs["status_filter"]] == ["matched"]
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 25


@pytest.mark.asyncio
async def test_confirm_import_response_commits_and_triggers_execute() -> None:
    """Confirming an import should commit before triggering Step 4 execution."""
    service = AsyncMock()
    service.confirm_import.return_value = _import_job(ImportJobStatus.IMPORTING)
    session = AsyncMock()
    trigger_execute = MagicMock()

    response = await confirm_import_response(
        service,
        session=session,
        job_id=42,
        body=ConfirmImportRequest(series_ids=[1]),
        trigger_import_execute=trigger_execute,
    )

    assert response.id == 42
    assert response.status == ImportJobStatus.IMPORTING
    session.commit.assert_awaited_once()
    trigger_execute.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_confirm_import_response_commits_review_snapshot_on_capacity_block() -> None:
    """A typed capacity block must be durable without scheduling execution."""
    service = AsyncMock()
    service.confirm_import.side_effect = ManagedCopyPreflightError(
        ManagedCopyPreflightFailure.CAPACITY_INSUFFICIENT,
        "The selected managed library root does not have enough free space for this import.",
        snapshot=ManagedCopyCapacitySnapshot(
            schema_version=1,
            stage="confirmation",
            target_library_root_id=9,
            selected_source_bytes=20 * 1024**3,
            reserve_bytes=2 * 1024**3,
            required_bytes=22 * 1024**3,
            free_bytes=22 * 1024**3 - 1,
            status="insufficient",
        ),
    )
    session = AsyncMock()
    trigger_execute = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await confirm_import_response(
            service,
            session=session,
            job_id=42,
            body=ConfirmImportRequest(series_ids=[1]),
            trigger_import_execute=trigger_execute,
        )

    assert exc_info.value.status_code == 409
    session.commit.assert_awaited_once()
    trigger_execute.assert_not_called()


@pytest.mark.asyncio
async def test_resume_import_job_response_triggers_only_active_import_states() -> None:
    """Resume should retrigger background work only for resumable active phases."""
    service = AsyncMock()
    session = AsyncMock()
    trigger_resume = MagicMock()

    service.resume_job.return_value = _import_job(ImportJobStatus.REVIEW)
    await resume_import_job_response(
        service,
        session=session,
        job_id=42,
        trigger_import_resume=trigger_resume,
    )
    trigger_resume.assert_not_called()

    service.resume_job.return_value = _import_job(ImportJobStatus.FILE_MATCHING)
    response = await resume_import_job_response(
        service,
        session=session,
        job_id=42,
        trigger_import_resume=trigger_resume,
    )

    assert response.status == ImportJobStatus.FILE_MATCHING
    trigger_resume.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_retry_failed_response_does_not_trigger_empty_retry() -> None:
    """A durable blocked-source result must not launch an empty Step 4 task."""
    service = AsyncMock()
    service.retry_failed_series.return_value = (
        _import_job(ImportJobStatus.COMPLETED),
        0,
    )
    session = AsyncMock()
    trigger_execute = MagicMock()

    response = await retry_failed_series_response(
        service,
        session=session,
        job_id=42,
        trigger_import_execute=trigger_execute,
    )

    assert response.retrying_count == 0
    session.commit.assert_awaited_once()
    trigger_execute.assert_not_called()


@pytest.mark.asyncio
async def test_clear_import_history_response_deletes_only_terminal_jobs(
    db_session: AsyncSession,
) -> None:
    """Clearing import history should spare active review/scan jobs."""
    db_session.add_all(
        [
            ImportJob(
                source_path="/tmp/completed",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
            ),
            ImportJob(
                source_path="/tmp/failed",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.FAILED,
            ),
            ImportJob(
                source_path="/tmp/review",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.REVIEW,
            ),
        ]
    )
    await db_session.commit()

    response = await clear_import_history_response(db_session)

    assert response == {"deleted": 2}
    remaining = await db_session.scalars(select(ImportJob.source_path))
    assert set(remaining.all()) == {"/tmp/review"}


@pytest.mark.asyncio
async def test_cancel_response_keeps_runtime_state_while_rollback_is_pending() -> None:
    """Deferred Story Arc rollback must retain the job runner's live state."""
    service = AsyncMock()
    service.cancel_job.return_value = "rollback_pending"
    session = AsyncMock()
    purge_runtime_state = MagicMock()

    response = await cancel_import_job_response(
        service,
        session=session,
        job_id=42,
        purge_import_runtime_state=purge_runtime_state,
    )

    assert response is not None
    assert response.status == "rollback_pending"
    assert "remains in history" in response.message
    session.commit.assert_awaited_once()
    purge_runtime_state.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_response_rejects_deleting_incomplete_rollback_evidence() -> None:
    service = AsyncMock()
    service.cancel_job.return_value = "rollback_incomplete"
    session = AsyncMock()
    purge_runtime_state = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await cancel_import_job_response(
            service,
            session=session,
            job_id=42,
            purge_import_runtime_state=purge_runtime_state,
        )

    assert exc_info.value.status_code == 409
    assert "requires manual recovery" in str(exc_info.value.detail)
    assert "remains in history" in str(exc_info.value.detail)
    session.commit.assert_awaited_once()
    purge_runtime_state.assert_not_called()
