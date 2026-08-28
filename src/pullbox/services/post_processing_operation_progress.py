"""Adapter from post-processing lifecycle state to shared operation progress."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pullbox.models.download import DownloadClientType, DownloadState
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


def _snapshot_value(snapshot: Any | None, name: str) -> object | None:
    return getattr(snapshot, name, None) if snapshot is not None else None


def _phase_value(snapshot: Any | None) -> str:
    phase = _snapshot_value(snapshot, "phase")
    value = getattr(phase, "value", phase)
    return str(value or "post_processing")


def _phase_label(snapshot: Any | None) -> str:
    label = _snapshot_value(snapshot, "phase_label")
    if isinstance(label, str) and label:
        return label
    return _phase_value(snapshot).replace("_", " ").capitalize()


def _source_label(client_type: DownloadClientType) -> str:
    labels = {
        DownloadClientType.AIRDCPP: "AirDC++",
        DownloadClientType.DIRECT: "Direct download",
        DownloadClientType.QBITTORRENT: "qBittorrent",
        DownloadClientType.SABNZBD: "SABnzbd",
        DownloadClientType.NZBGET: "NZBGet",
    }
    return labels.get(client_type, client_type.value.replace("_", " ").title())


def build_post_processing_operation_update(
    download: DownloadHistory,
    snapshot: Any | None = None,
) -> OperationProgressUpdate:
    """Build truthful post-processing progress without invented phase percentages."""
    phase = _phase_value(snapshot)
    is_failed = DownloadState(download.state) is DownloadState.FAILED
    is_complete = phase == "import_complete" or download.imported_at is not None
    if is_failed:
        state = OperationProgressState.FAILED
        tone = OperationProgressTone.DANGER
    elif is_complete:
        state = OperationProgressState.COMPLETED
        tone = OperationProgressTone.SUCCESS
    else:
        state = OperationProgressState.RUNNING
        tone = OperationProgressTone.INFO

    total = _snapshot_value(snapshot, "transfer_total_bytes")
    current = _snapshot_value(snapshot, "transfer_done_bytes")
    total_bytes = int(total) if isinstance(total, int | float) and total > 0 else None
    current_bytes = int(current) if isinstance(current, int | float) and current >= 0 else None
    if is_complete:
        total_bytes = total_bytes or download.file_size
        current_bytes = total_bytes or current_bytes

    rate = _snapshot_value(snapshot, "transfer_speed_bytes")
    eta = _snapshot_value(snapshot, "transfer_eta_seconds")
    message = download.error_message if is_failed else _phase_label(snapshot)
    return OperationProgressUpdate(
        operation_type=OperationProgressType.POST_PROCESSING,
        operation_key=str(download.id),
        group_key="downloads",
        revision=None,
        state=state,
        phase="failed" if is_failed else phase,
        title=download.title,
        message=message or "Post-processing",
        source_label=_source_label(DownloadClientType(download.download_client)),
        detail_url="/post-processing",
        visibility=OperationProgressVisibility.PROMINENT,
        tone=tone,
        attention_required=is_failed,
        overall=OperationProgressMeasure(
            current=current_bytes,
            total=total_bytes,
            percent=100.0 if is_complete else None,
            unit="bytes" if total_bytes is not None or current_bytes is not None else None,
        ),
        rate=float(rate) if isinstance(rate, int | float) else None,
        rate_unit="bytes_per_second",
        eta_seconds=int(eta) if isinstance(eta, int | float) else None,
        detail_snapshot={
            "download_id": download.id,
            "issue_id": download.issue_id,
            "client": download.download_client.value,
        },
        started_at=download.post_processing_claimed_at or download.completed_at,
    )


async def project_post_processing_operation_progress(
    session: AsyncSession,
    download: DownloadHistory,
    snapshot: Any | None = None,
) -> None:
    """Persist one post-processing operation through the shared publisher."""
    from pullbox.services.operation_progress import publish_operation_progress

    await publish_operation_progress(
        session,
        build_post_processing_operation_update(download, snapshot),
    )


def queue_post_processing_phase(download: DownloadHistory, phase: Any) -> None:
    """Queue a coalesced phase update from synchronous orchestration callbacks."""
    snapshot = SimpleNamespace(
        phase=phase,
        phase_label=getattr(phase, "label", str(phase)),
        transfer_done_bytes=None,
        transfer_total_bytes=None,
        transfer_speed_bytes=None,
        transfer_eta_seconds=None,
    )
    queue_post_processing_snapshot(download, snapshot)


def queue_post_processing_snapshot(
    download: DownloadHistory,
    snapshot: Any | None,
) -> None:
    """Schedule coalesced persistence without blocking file-processing callbacks."""
    from pullbox.services.operation_progress_dispatch import queue_operation_progress

    task = asyncio.get_running_loop().create_task(
        queue_operation_progress(build_post_processing_operation_update(download, snapshot))
    )
    task.add_done_callback(_consume_queue_error)


def _consume_queue_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()
