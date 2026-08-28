"""Protocol classification tests for release search results."""

import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import ReleaseResult


def _release(**overrides: object) -> ReleaseResult:
    values: dict[str, object] = {
        "title": "Example Comic 001",
        "indexer_name": "Example Indexer",
        "download_url": "https://example.test/download/1",
        "size_bytes": 1_000,
        "age_days": 1,
        "seeders": None,
        "leechers": None,
        "grabs": 3,
        "category": "Books/Comics",
        "published_at": None,
    }
    values.update(overrides)
    return ReleaseResult(**values)  # type: ignore[arg-type]


def test_legacy_usenet_release_derives_protocol() -> None:
    release = _release(is_torrent=False)

    assert release.protocol is AcquisitionProtocol.USENET
    assert release.is_torrent is False


def test_legacy_torrent_release_derives_protocol() -> None:
    release = _release(is_torrent=True)

    assert release.protocol is AcquisitionProtocol.TORRENT
    assert release.is_torrent is True


@pytest.mark.parametrize("protocol", list(AcquisitionProtocol))
def test_explicit_protocol_is_authoritative(protocol: AcquisitionProtocol) -> None:
    release = _release(protocol=protocol)

    assert release.protocol is protocol
    assert release.is_torrent is (protocol is AcquisitionProtocol.TORRENT)
