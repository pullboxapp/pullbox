"""Helpers for translating download-client status into monitor DB updates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pullbox.models.download import DownloadState


def build_status_update(
    *,
    download_id: int,
    external_id: object,
    status: Any,
    existing_path: object,
    is_stall_state: bool,
    event_logger: Any,
) -> dict[str, object] | None:
    """Build the monitor write-phase update for one client status."""
    raw_progress = getattr(status, "progress", None)
    if raw_progress is None:
        raw_progress = 1.0 if status.state == "completed" else 0.0
    progress = max(0.0, min(float(raw_progress), 1.0))
    raw_size = getattr(status, "size_bytes", None)
    size_bytes = int(raw_size) if raw_size is not None else None
    update: dict[str, object] = {
        "id": download_id,
        "client_state": status.client_state,
        "progress_snapshot": {
            "progress": progress,
            "speed_bytes": getattr(status, "speed_bytes", None),
            "eta_seconds": getattr(status, "eta_seconds", None),
            "size_bytes": size_bytes,
            "bytes_transferred": round(progress * size_bytes) if size_bytes is not None else None,
            "client_state": status.client_state,
            "is_indeterminate": size_bytes is None and progress <= 0,
        },
    }

    if status.state == "completed":
        event_logger.info(
            "download_completion_detected",
            download_id=download_id,
            external_id=external_id,
            client_state=status.client_state,
            downloaded_path=status.downloaded_path,
        )
        update["state"] = DownloadState.COMPLETED
        update["completed_at"] = datetime.now(UTC)
        if status.downloaded_path:
            update["downloaded_path"] = status.downloaded_path
    elif status.state == "failed":
        update["failed"] = True
        update["error_message"] = status.error_message
    elif status.state == "downloading":
        update["state"] = DownloadState.DOWNLOADING
        if status.downloaded_path and not existing_path:
            update["downloaded_path"] = status.downloaded_path
    elif status.state == "finalizing":
        update["state"] = DownloadState.FINALIZING
        if status.downloaded_path and not existing_path:
            update["downloaded_path"] = status.downloaded_path
    elif status.state == "paused":
        update["state"] = DownloadState.PAUSED

    # Always send a heartbeat so Phase 3 can refresh updated_at, preventing
    # false stall detection on long-running downloads.
    if "state" not in update and "failed" not in update and not is_stall_state:
        update["heartbeat"] = True
    return update


def build_status_check_error_update(
    *,
    download_id: int,
    external_id: object,
    client_type: object,
    issue_id: object,
    error: Exception,
    event_logger: Any,
) -> dict[str, object] | None:
    """Build a monitor update for a failed client status check, if actionable."""
    error_msg = str(error).lower()
    if "not found" in error_msg:
        event_logger.warning(
            "download_removed_externally",
            download_id=download_id,
            external_id=external_id,
            client_type=str(client_type),
        )
        return {
            "id": download_id,
            "removed_externally": True,
            "error_message": "Download was removed from the client externally",
            "issue_id": issue_id,
        }

    event_logger.exception(
        "download_status_check_failed",
        download_id=download_id,
        external_id=external_id,
    )
    return None
