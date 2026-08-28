"""Shared progress projection contracts for post-processing."""

from types import SimpleNamespace

import pytest

from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.operation_progress import OperationProgressState, OperationProgressType
from pullbox.services.post_processing_operation_progress import (
    build_post_processing_operation_update,
)
from pullbox.tasks.post_processing_progress import PostProcessingPhase


def _download(*, state: DownloadState = DownloadState.POST_PROCESSING) -> DownloadHistory:
    return DownloadHistory(
        id=17,
        issue_id=9,
        title="Example Comic 001.cbz",
        download_url="https://example.test/example.cbz",
        download_client=DownloadClientType.SABNZBD,
        state=state,
        file_size=100_000_000,
    )


def test_transfer_progress_uses_real_bytes_rate_and_eta() -> None:
    snapshot = SimpleNamespace(
        phase=PostProcessingPhase.TRANSFERRING_FILE,
        phase_label="Transferring file",
        transfer_done_bytes=25_000_000,
        transfer_total_bytes=100_000_000,
        transfer_speed_bytes=2_000_000,
        transfer_eta_seconds=38,
    )

    update = build_post_processing_operation_update(_download(), snapshot)

    assert update.operation_type is OperationProgressType.POST_PROCESSING
    assert update.operation_key == "17"
    assert update.state is OperationProgressState.RUNNING
    assert update.phase == "transferring_file"
    assert update.overall.current == 25_000_000
    assert update.overall.total == 100_000_000
    assert update.overall.percent is None
    assert update.rate == 2_000_000
    assert update.eta_seconds == 38


def test_non_transfer_phase_is_truthfully_indeterminate() -> None:
    snapshot = SimpleNamespace(
        phase=PostProcessingPhase.VALIDATING_FILES,
        phase_label="Validating files",
        transfer_done_bytes=None,
        transfer_total_bytes=None,
        transfer_speed_bytes=None,
        transfer_eta_seconds=None,
    )

    update = build_post_processing_operation_update(_download(), snapshot)

    assert update.phase == "validating_files"
    assert update.overall.current is None
    assert update.overall.total is None
    assert update.overall.percent is None


def test_failed_post_processing_requires_attention() -> None:
    download = _download(state=DownloadState.FAILED)
    download.error_message = "Destination is not writable"

    update = build_post_processing_operation_update(download)

    assert update.state is OperationProgressState.FAILED
    assert update.attention_required is True
    assert update.message == "Destination is not writable"


def test_completed_post_processing_reports_terminal_completion() -> None:
    download = _download(state=DownloadState.COMPLETED)
    snapshot = SimpleNamespace(
        phase=PostProcessingPhase.IMPORT_COMPLETE,
        phase_label="Import complete",
        transfer_done_bytes=100_000_000,
        transfer_total_bytes=100_000_000,
        transfer_speed_bytes=None,
        transfer_eta_seconds=0,
    )

    update = build_post_processing_operation_update(download, snapshot)

    assert update.state is OperationProgressState.COMPLETED
    assert update.overall.percent == pytest.approx(100.0)
