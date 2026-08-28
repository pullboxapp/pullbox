"""Shared progress adapters for manual issue imports and orphan recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import OrphanRecoveryProgressResponse
    from pullbox.schemas.issue import ManualFileImportProgressResponse


def _state_tone_attention(
    state: str,
) -> tuple[OperationProgressState, OperationProgressTone, bool]:
    if state == "completed":
        return OperationProgressState.COMPLETED, OperationProgressTone.SUCCESS, False
    if state == "cancelled":
        return OperationProgressState.CANCELLED, OperationProgressTone.WARNING, False
    if state == "safety_blocked":
        return OperationProgressState.PAUSED, OperationProgressTone.WARNING, True
    if state == "failed":
        return OperationProgressState.FAILED, OperationProgressTone.DANGER, True
    if state in {"paused", "pausing"}:
        return OperationProgressState.PAUSED, OperationProgressTone.WARNING, False
    return OperationProgressState.RUNNING, OperationProgressTone.INFO, False


def _current_item(progress: Any) -> OperationItemProgress | None:
    name = getattr(progress, "current_file_name", None)
    if not name:
        return None
    stage = str(getattr(progress, "current_file_stage", None) or "preparing")
    return OperationItemProgress(
        key=str(name),
        label=str(name),
        phase=stage,
        message=str(getattr(progress, "message", "") or ""),
        measure=OperationProgressMeasure(
            current=getattr(progress, "current_file_progress_current", None),
            total=getattr(progress, "current_file_progress_total", None),
            percent=getattr(progress, "current_file_progress_pct", None),
            unit=getattr(progress, "current_file_progress_unit", None),
        ),
    )


def build_issue_import_operation_update(
    progress: ManualFileImportProgressResponse,
) -> OperationProgressUpdate:
    """Build a shared projection for one manually imported issue file."""
    state, tone, attention = _state_tone_attention(progress.state)
    message = progress.error_message or progress.message
    percent = progress.current_file_progress_pct
    if state is OperationProgressState.COMPLETED:
        percent = 100
    return OperationProgressUpdate(
        operation_type=OperationProgressType.ISSUE_IMPORT,
        operation_key=str(progress.issue_id),
        group_key="imports",
        revision=None,
        state=state,
        phase=str(progress.current_file_stage or progress.state),
        title=f"Manual issue import #{progress.issue_id}",
        message=message or "Manual issue import",
        source_label="Manual import",
        detail_url=f"/issues/{progress.issue_id}",
        visibility=OperationProgressVisibility.PROMINENT,
        tone=tone,
        attention_required=attention,
        overall=OperationProgressMeasure(percent=percent, unit="files"),
        item=_current_item(progress),
        detail_snapshot={"issue_id": progress.issue_id},
    )


def build_orphan_recovery_operation_update(
    progress: OrphanRecoveryProgressResponse,
) -> OperationProgressUpdate:
    """Build a shared projection for an orphaned-series recovery run."""
    state, tone, attention = _state_tone_attention(progress.state)
    file_index = progress.file_index
    completed_files = max(file_index - 1, 0) if file_index is not None else None
    if state is OperationProgressState.COMPLETED:
        completed_files = progress.total_files
    return OperationProgressUpdate(
        operation_type=OperationProgressType.ORPHAN_RECOVERY,
        operation_key=str(progress.imported_series_id),
        group_key="imports",
        revision=None,
        state=state,
        phase=str(progress.current_file_stage or progress.state),
        title=f"Recovering imported series #{progress.imported_series_id}",
        message=progress.error_message or progress.message,
        source_label="Import recovery",
        detail_url="/import/orphaned",
        visibility=OperationProgressVisibility.PROMINENT,
        tone=tone,
        attention_required=attention,
        overall=OperationProgressMeasure(
            current=completed_files,
            total=progress.total_files,
            unit="files",
        ),
        item=_current_item(progress),
        detail_snapshot={"imported_series_id": progress.imported_series_id},
    )


async def project_issue_import_operation_progress(
    session: AsyncSession,
    progress: ManualFileImportProgressResponse,
) -> None:
    from pullbox.services.operation_progress import publish_operation_progress

    await publish_operation_progress(session, build_issue_import_operation_update(progress))


async def project_orphan_recovery_operation_progress(
    session: AsyncSession,
    progress: OrphanRecoveryProgressResponse,
) -> None:
    from pullbox.services.operation_progress import publish_operation_progress

    await publish_operation_progress(session, build_orphan_recovery_operation_update(progress))
