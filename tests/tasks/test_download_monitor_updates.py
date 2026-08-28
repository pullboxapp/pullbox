"""Download monitor update-builder characterization tests."""

from __future__ import annotations

from types import SimpleNamespace

from pullbox.models.download import DownloadState


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))

    def exception(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


def test_completed_status_builds_completed_update_and_logs_detection() -> None:
    """Completed client statuses should carry final path and completed timestamp."""
    from pullbox.tasks import download_monitor_updates

    fake_logger = _FakeLogger()
    status = SimpleNamespace(
        state="completed",
        client_state="Completed",
        downloaded_path="/downloads/book.cbz",
        error_message=None,
    )

    update = download_monitor_updates.build_status_update(
        download_id=12,
        external_id="abc123",
        status=status,
        existing_path=None,
        is_stall_state=False,
        event_logger=fake_logger,
    )

    assert update is not None
    assert update["id"] == 12
    assert update["state"] == DownloadState.COMPLETED
    assert update["downloaded_path"] == "/downloads/book.cbz"
    assert "completed_at" in update
    assert update["progress_snapshot"] == {
        "progress": 1.0,
        "speed_bytes": None,
        "eta_seconds": None,
        "size_bytes": None,
        "bytes_transferred": None,
        "client_state": "Completed",
        "is_indeterminate": False,
    }
    assert fake_logger.events == [
        (
            "download_completion_detected",
            {
                "download_id": 12,
                "external_id": "abc123",
                "client_state": "Completed",
                "downloaded_path": "/downloads/book.cbz",
            },
        )
    ]


def test_finalizing_status_builds_finalizing_update() -> None:
    """Client-side repair/extract phases should remain active but not downloading."""
    from pullbox.tasks import download_monitor_updates

    status = SimpleNamespace(
        state="finalizing",
        client_state="Repairing",
        downloaded_path="/downloads/book",
        error_message=None,
    )

    update = download_monitor_updates.build_status_update(
        download_id=14,
        external_id="nzo123",
        status=status,
        existing_path=None,
        is_stall_state=False,
        event_logger=_FakeLogger(),
    )

    assert update == {
        "id": 14,
        "client_state": "Repairing",
        "progress_snapshot": {
            "progress": 0.0,
            "speed_bytes": None,
            "eta_seconds": None,
            "size_bytes": None,
            "bytes_transferred": None,
            "client_state": "Repairing",
            "is_indeterminate": True,
        },
        "state": DownloadState.FINALIZING,
        "downloaded_path": "/downloads/book",
    }


def test_unmapped_status_preserves_existing_no_op_update_shape() -> None:
    """Unmapped client statuses should preserve the existing no-op update shape."""
    from pullbox.tasks import download_monitor_updates

    status = SimpleNamespace(
        state="queued",
        client_state="Queued",
        downloaded_path=None,
        error_message=None,
    )

    healthy = download_monitor_updates.build_status_update(
        download_id=44,
        external_id="abc123",
        status=status,
        existing_path=None,
        is_stall_state=False,
        event_logger=_FakeLogger(),
    )
    stalled = download_monitor_updates.build_status_update(
        download_id=44,
        external_id="abc123",
        status=status,
        existing_path=None,
        is_stall_state=True,
        event_logger=_FakeLogger(),
    )

    expected = {
        "id": 44,
        "client_state": "Queued",
        "progress_snapshot": {
            "progress": 0.0,
            "speed_bytes": None,
            "eta_seconds": None,
            "size_bytes": None,
            "bytes_transferred": None,
            "client_state": "Queued",
            "is_indeterminate": True,
        },
    }
    assert healthy == {**expected, "heartbeat": True}
    assert stalled == expected


def test_not_found_status_error_builds_removed_externally_update() -> None:
    """Client not-found errors should mark the download as externally removed."""
    from pullbox.tasks import download_monitor_updates

    fake_logger = _FakeLogger()

    update = download_monitor_updates.build_status_check_error_update(
        download_id=55,
        external_id="deadbeef",
        client_type="qbittorrent",
        issue_id=99,
        error=RuntimeError("torrent not found"),
        event_logger=fake_logger,
    )

    assert update == {
        "id": 55,
        "removed_externally": True,
        "error_message": "Download was removed from the client externally",
        "issue_id": 99,
    }
    assert fake_logger.events == [
        (
            "download_removed_externally",
            {
                "download_id": 55,
                "external_id": "deadbeef",
                "client_type": "qbittorrent",
            },
        )
    ]


def test_other_status_error_logs_exception_without_update() -> None:
    """Non-not-found status failures should log and leave the DB untouched."""
    from pullbox.tasks import download_monitor_updates

    fake_logger = _FakeLogger()

    update = download_monitor_updates.build_status_check_error_update(
        download_id=55,
        external_id="deadbeef",
        client_type="sabnzbd",
        issue_id=99,
        error=TimeoutError("client timed out"),
        event_logger=fake_logger,
    )

    assert update is None
    assert fake_logger.events == [
        (
            "download_status_check_failed",
            {"download_id": 55, "external_id": "deadbeef"},
        )
    ]
