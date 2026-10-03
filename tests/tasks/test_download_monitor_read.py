"""Download monitor read-phase characterization tests."""

from __future__ import annotations

from types import SimpleNamespace

from pullbox.models.download import DownloadState


def test_build_poll_item_snapshots_detached_download_fields() -> None:
    """Poll items should copy every field the no-session poll phase needs."""
    from pullbox.tasks import download_monitor_read

    download = SimpleNamespace(
        id=11,
        external_id="abc",
        title="Batman 001.cbz",
        download_client="sabnzbd",
        download_client_config_id=4,
        downloaded_path="/downloads/Batman 001.cbz",
        issue_id=22,
        retry_count=1,
        max_retries=3,
    )

    assert download_monitor_read.build_poll_item(download) == {
        "id": 11,
        "external_id": "abc",
        "title": "Batman 001.cbz",
        "download_client": "sabnzbd",
        "download_client_config_id": 4,
        "downloaded_path": "/downloads/Batman 001.cbz",
        "issue_id": 22,
        "retry_count": 1,
        "max_retries": 3,
    }


def test_active_download_states_match_monitor_contract() -> None:
    """The read phase should only poll transient active download states."""
    from pullbox.tasks import download_monitor_read

    assert download_monitor_read.ACTIVE_DOWNLOAD_STATES == (
        DownloadState.SENT,
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
        DownloadState.PAUSED,
    )
