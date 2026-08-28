"""Operation progress adapters for manual imports and orphan recovery."""

from pullbox.models.operation_progress import OperationProgressState, OperationProgressType
from pullbox.schemas.import_job import OrphanRecoveryProgressResponse
from pullbox.schemas.issue import ManualFileImportProgressResponse
from pullbox.services.secondary_operation_progress import (
    build_issue_import_operation_update,
    build_orphan_recovery_operation_update,
)


def test_manual_issue_import_maps_file_stage_and_progress() -> None:
    update = build_issue_import_operation_update(
        ManualFileImportProgressResponse(
            issue_id=12,
            state="running",
            message="Importing selected file...",
            current_file_name="Batman 001.cbr",
            current_file_stage="transferring",
            current_file_progress_current=25,
            current_file_progress_total=100,
            current_file_progress_pct=25,
            current_file_progress_unit="bytes",
            file_index=1,
            total_files=1,
        )
    )

    assert update.operation_type is OperationProgressType.ISSUE_IMPORT
    assert update.operation_key == "12"
    assert update.state is OperationProgressState.RUNNING
    assert update.overall.percent == 25
    assert update.item is not None
    assert update.item.label == "Batman 001.cbr"
    assert update.item.phase == "transferring"


def test_manual_issue_import_safety_block_requires_attention() -> None:
    update = build_issue_import_operation_update(
        ManualFileImportProgressResponse(
            issue_id=12,
            state="safety_blocked",
            message="Safety approval required.",
            error_message="File exceeds configured limit.",
        )
    )

    assert update.state is OperationProgressState.PAUSED
    assert update.attention_required is True
    assert update.message == "File exceeds configured limit."


def test_orphan_recovery_maps_batch_and_current_file_progress() -> None:
    update = build_orphan_recovery_operation_update(
        OrphanRecoveryProgressResponse(
            imported_series_id=31,
            state="running",
            message="Recovering file 2 of 4",
            current_file_name="Annual 001.cbz",
            current_file_stage="writing_metadata",
            current_file_progress_current=2,
            current_file_progress_total=5,
            current_file_progress_pct=40,
            current_file_progress_unit="steps",
            file_index=2,
            total_files=4,
        )
    )

    assert update.operation_type is OperationProgressType.ORPHAN_RECOVERY
    assert update.overall.current == 1
    assert update.overall.total == 4
    assert update.item is not None
    assert update.item.measure.percent == 40


def test_orphan_recovery_failure_is_terminal_attention() -> None:
    update = build_orphan_recovery_operation_update(
        OrphanRecoveryProgressResponse(
            imported_series_id=31,
            state="failed",
            message="Recovery failed.",
            error_message="Destination unavailable",
        )
    )

    assert update.state is OperationProgressState.FAILED
    assert update.attention_required is True
    assert update.message == "Destination unavailable"
