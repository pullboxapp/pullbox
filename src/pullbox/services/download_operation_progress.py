"""Adapter from download lifecycle data to shared operation progress."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pullbox.models.download import DownloadState
from pullbox.models.operation_progress import (
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)
from pullbox.services.operation_progress import (
    OperationProgressMeasure,
    OperationProgressUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.download import DownloadHistory


def _progress_value(progress: Any, name: str, default: object = None) -> object:
    if isinstance(progress, Mapping):
        return progress.get(name, default)
    return getattr(progress, name, default)


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_value(value: object) -> int | None:
    parsed = _float_value(value)
    return int(parsed) if parsed is not None else None


def _shared_state(state: DownloadState) -> OperationProgressState:
    if state in {DownloadState.QUEUED, DownloadState.SENT}:
        return OperationProgressState.QUEUED
    if state in {
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
        DownloadState.POST_PROCESSING,
    }:
        return OperationProgressState.RUNNING
    if state is DownloadState.PAUSED:
        return OperationProgressState.PAUSED
    if state is DownloadState.RETRY_PENDING:
        return OperationProgressState.RETRYING
    if state is DownloadState.FAILED:
        return OperationProgressState.FAILED
    return OperationProgressState.COMPLETED


def _default_source_label(download: DownloadHistory) -> str:
    return download.download_client.value.replace("_", " ").title()


def build_download_operation_update(
    download: DownloadHistory,
    progress: Any | None = None,
) -> OperationProgressUpdate:
    """Build a truthful download projection from durable state and client metrics."""
    shared_state = _shared_state(DownloadState(download.state))
    size_bytes = _int_value(
        _progress_value(progress, "size_bytes") if progress is not None else None
    )
    bytes_transferred = _int_value(
        _progress_value(progress, "bytes_transferred") if progress is not None else None
    )
    progress_fraction = _float_value(
        _progress_value(progress, "progress") if progress is not None else None
    )
    is_indeterminate = bool(
        _progress_value(progress, "is_indeterminate", False) if progress is not None else True
    )
    if bytes_transferred is None and size_bytes and progress_fraction is not None:
        bytes_transferred = round(progress_fraction * size_bytes)
    percent = None
    if not is_indeterminate and progress_fraction is not None:
        percent = progress_fraction * 100
    if shared_state is OperationProgressState.COMPLETED:
        percent = 100.0

    client_state = _progress_value(progress, "client_state") if progress is not None else None
    source_label = _progress_value(progress, "source_label") if progress is not None else None
    source_slow = (
        bool(_progress_value(progress, "source_slow", False)) if progress is not None else False
    )
    attention_required = shared_state is OperationProgressState.FAILED
    tone = OperationProgressTone.INFO
    if attention_required:
        tone = OperationProgressTone.DANGER
    elif source_slow or shared_state in {
        OperationProgressState.PAUSED,
        OperationProgressState.RETRYING,
    }:
        tone = OperationProgressTone.WARNING
    elif shared_state is OperationProgressState.COMPLETED:
        tone = OperationProgressTone.SUCCESS

    return OperationProgressUpdate(
        operation_type=OperationProgressType.DOWNLOAD,
        operation_key=str(download.id),
        group_key="downloads",
        revision=None,
        state=shared_state,
        phase=str(client_state or download.state.value).lower().replace(" ", "_"),
        title=download.title,
        message=str(client_state or download.error_message or ""),
        source_label=str(source_label or _default_source_label(download)),
        detail_url="/downloads",
        visibility=OperationProgressVisibility.PROMINENT,
        tone=tone,
        attention_required=attention_required,
        overall=OperationProgressMeasure(
            current=bytes_transferred,
            total=size_bytes if size_bytes is not None else download.file_size,
            percent=percent,
            unit="bytes",
        ),
        rate=(
            _float_value(_progress_value(progress, "speed_bytes")) if progress is not None else None
        ),
        rate_unit="bytes_per_second",
        eta_seconds=(
            _int_value(_progress_value(progress, "eta_seconds")) if progress is not None else None
        ),
        detail_snapshot={
            "download_id": download.id,
            "issue_id": download.issue_id,
            "client": download.download_client.value,
            "source_slow": source_slow,
        },
        started_at=download.sent_at or download.created_at,
    )


async def project_download_operation_progress(
    session: AsyncSession,
    download: DownloadHistory,
    progress: Any | None = None,
) -> None:
    """Persist one download's latest progress through the shared publisher."""
    from pullbox.services.operation_progress import publish_operation_progress

    await publish_operation_progress(
        session,
        build_download_operation_update(download, progress),
    )
