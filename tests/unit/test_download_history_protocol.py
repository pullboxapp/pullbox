"""Protocol and exact-client identity tests for download history."""

import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.download import DownloadClientType, DownloadHistory


@pytest.mark.parametrize("client_type", list(DownloadClientType))
def test_legacy_history_construction_derives_protocol(
    client_type: DownloadClientType,
) -> None:
    history = DownloadHistory(
        issue_id=1,
        title="Example Comic 001",
        download_url="https://example.test/download/1",
        download_client=client_type,
    )

    assert history.protocol is client_type.acquisition_protocol
    assert history.protocol in AcquisitionProtocol


def test_history_accepts_exact_download_client_config_id() -> None:
    history = DownloadHistory(
        issue_id=1,
        title="Example Comic 001",
        download_url="https://example.test/download/1",
        download_client=DownloadClientType.SABNZBD,
        download_client_config_id=42,
    )

    assert history.download_client_config_id == 42


def test_legacy_history_construction_accepts_enum_member_name() -> None:
    history = DownloadHistory(
        issue_id=1,
        title="Example Comic 001",
        download_url="https://example.test/download/1",
        download_client="SABNZBD",  # type: ignore[arg-type]
    )

    assert history.download_client is DownloadClientType.SABNZBD
    assert history.protocol is AcquisitionProtocol.USENET
