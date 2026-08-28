"""Tests for download-client progress projection."""

from __future__ import annotations

from types import SimpleNamespace

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.operation_progress import OperationProgressState
from pullbox.services.download_operation_progress import build_download_operation_update


def _download(state: DownloadState = DownloadState.DOWNLOADING) -> DownloadHistory:
    return DownloadHistory(
        id=7,
        issue_id=99,
        title="Batman 001",
        download_url="https://indexer.example/download/7",
        download_client=DownloadClientType.QBITTORRENT,
        protocol=AcquisitionProtocol.TORRENT,
        state=state,
    )


def test_download_progress_maps_bytes_speed_eta_and_source() -> None:
    update = build_download_operation_update(
        _download(),
        SimpleNamespace(
            progress=0.5,
            speed_bytes=2048,
            eta_seconds=30,
            size_bytes=8192,
            bytes_transferred=4096,
            client_state="Downloading",
            source_label="qBittorrent",
            is_indeterminate=False,
            source_slow=False,
        ),
    )

    assert update.state is OperationProgressState.RUNNING
    assert update.operation_key == "7"
    assert update.overall.current == 4096
    assert update.overall.total == 8192
    assert update.overall.percent == 50
    assert update.rate == 2048
    assert update.rate_unit == "bytes_per_second"
    assert update.eta_seconds == 30
    assert update.source_label == "qBittorrent"


def test_unknown_download_size_stays_indeterminate() -> None:
    update = build_download_operation_update(
        _download(),
        SimpleNamespace(
            progress=0,
            speed_bytes=None,
            eta_seconds=None,
            size_bytes=None,
            bytes_transferred=1024,
            client_state="Receiving",
            source_label="Direct download",
            is_indeterminate=True,
            source_slow=False,
        ),
    )

    assert update.overall.current == 1024
    assert update.overall.total is None
    assert update.overall.percent is None


def test_non_running_download_states_drop_stale_rate_and_eta() -> None:
    for state in (
        DownloadState.QUEUED,
        DownloadState.RETRY_PENDING,
        DownloadState.FAILED,
    ):
        update = build_download_operation_update(
            _download(state),
            SimpleNamespace(
                progress=0.25,
                speed_bytes=2048,
                eta_seconds=30,
                size_bytes=8192,
                bytes_transferred=2048,
                client_state=state.value,
                source_label="Direct download",
                is_indeterminate=False,
                source_slow=False,
            ),
        )

        assert update.state is not OperationProgressState.RUNNING
        assert update.rate is None
        assert update.eta_seconds is None
