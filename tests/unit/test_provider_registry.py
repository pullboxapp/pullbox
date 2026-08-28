"""Unit tests for ProviderRegistry multi-client support (DCE-S.1).

Tests priority-based client selection for NZB and torrent protocols,
get_client_for_type lookups, and edge cases.

Run:
    pytest tests/unit/test_provider_registry.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pullbox.providers.base import ProviderRegistry


def _mock_client(client_type: str) -> MagicMock:
    """Create a mock DownloadClient with the given client_type."""
    client = MagicMock()
    client.client_type = client_type
    client.name = client_type
    return client


class TestGetNzbClient:
    """Tests for get_nzb_client() with priority-based selection."""

    def test_single_sabnzbd(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, sab, priority=50)

        assert reg.get_nzb_client() is sab

    def test_single_nzbget(self) -> None:
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, nzb, priority=50)

        assert reg.get_nzb_client() is nzb

    def test_two_nzb_clients_returns_lower_priority(self) -> None:
        """Lower priority number = higher priority."""
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, nzb, priority=10)

        assert reg.get_nzb_client() is nzb

    def test_two_nzb_clients_reversed_registration(self) -> None:
        """Priority wins regardless of registration order."""
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, nzb, priority=100)
        reg.register_download_client(2, sab, priority=10)

        assert reg.get_nzb_client() is sab

    def test_no_nzb_clients(self) -> None:
        reg = ProviderRegistry()
        qbt = _mock_client("qbittorrent")
        reg.register_download_client(1, qbt, priority=50)

        assert reg.get_nzb_client() is None

    def test_empty_registry(self) -> None:
        reg = ProviderRegistry()
        assert reg.get_nzb_client() is None


class TestGetTorrentClient:
    """Tests for get_torrent_client() with priority-based selection."""

    def test_single_qbittorrent(self) -> None:
        reg = ProviderRegistry()
        qbt = _mock_client("qbittorrent")
        reg.register_download_client(1, qbt, priority=50)

        assert reg.get_torrent_client() is qbt

    def test_single_transmission(self) -> None:
        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        reg.register_download_client(1, tr, priority=50)

        assert reg.get_torrent_client() is tr

    def test_single_deluge(self) -> None:
        reg = ProviderRegistry()
        dl = _mock_client("deluge")
        reg.register_download_client(1, dl, priority=50)

        assert reg.get_torrent_client() is dl

    def test_three_torrent_clients_returns_lowest_priority(self) -> None:
        reg = ProviderRegistry()
        qbt = _mock_client("qbittorrent")
        tr = _mock_client("transmission")
        dl = _mock_client("deluge")
        reg.register_download_client(1, qbt, priority=50)
        reg.register_download_client(2, tr, priority=10)
        reg.register_download_client(3, dl, priority=30)

        assert reg.get_torrent_client() is tr

    def test_no_torrent_clients(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, sab, priority=50)

        assert reg.get_torrent_client() is None


class TestGetClientForType:
    """Tests for get_client_for_type() — lookup by specific type string."""

    def test_nzbget_found(self) -> None:
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, nzb, priority=50)

        assert reg.get_client_for_type("nzbget") is nzb

    def test_transmission_found(self) -> None:
        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        reg.register_download_client(1, tr, priority=50)

        assert reg.get_client_for_type("transmission") is tr

    def test_deluge_found(self) -> None:
        reg = ProviderRegistry()
        dl = _mock_client("deluge")
        reg.register_download_client(1, dl, priority=50)

        assert reg.get_client_for_type("deluge") is dl

    def test_unknown_type_returns_none(self) -> None:
        reg = ProviderRegistry()
        assert reg.get_client_for_type("rtorrent") is None

    def test_multiple_clients_returns_first_match(self) -> None:
        """If somehow multiple clients of the same type exist, returns one."""
        reg = ProviderRegistry()
        sab1 = _mock_client("sabnzbd")
        sab2 = _mock_client("sabnzbd")
        reg.register_download_client(1, sab1, priority=50)
        reg.register_download_client(2, sab2, priority=10)

        result = reg.get_client_for_type("sabnzbd")
        assert result is not None
        assert result.client_type == "sabnzbd"


class TestGetDownloadClient:
    """Tests for exact persisted configuration lookup."""

    def test_returns_only_the_requested_configuration(self) -> None:
        reg = ProviderRegistry()
        first = _mock_client("sabnzbd")
        second = _mock_client("sabnzbd")
        reg.register_download_client(11, first, priority=50)
        reg.register_download_client(22, second, priority=10)

        assert reg.get_download_client(11) is first
        assert reg.get_download_client(22) is second
        assert reg.get_download_client(33) is None


class TestSamePriorityTiebreaker:
    """Two clients with the same priority → first registered wins."""

    def test_same_priority_first_registered_wins(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, nzb, priority=50)

        # First registered (sab) should win on tiebreak
        assert reg.get_nzb_client() is sab


class TestPriorityZero:
    """Priority 0 should beat priority 100."""

    def test_priority_zero_wins(self) -> None:
        reg = ProviderRegistry()
        low_prio = _mock_client("sabnzbd")
        high_prio = _mock_client("nzbget")
        reg.register_download_client(1, low_prio, priority=100)
        reg.register_download_client(2, high_prio, priority=0)

        assert reg.get_nzb_client() is high_prio


class TestMixedProtocols:
    """NZB and torrent clients don't interfere with each other."""

    def test_nzb_and_torrent_independent(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        qbt = _mock_client("qbittorrent")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, qbt, priority=50)

        assert reg.get_nzb_client() is sab
        assert reg.get_torrent_client() is qbt

    def test_all_five_clients_registered(self) -> None:
        """All 5 client types coexist, correct selection for each protocol."""
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        nzb = _mock_client("nzbget")
        qbt = _mock_client("qbittorrent")
        tr = _mock_client("transmission")
        dl = _mock_client("deluge")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, nzb, priority=10)
        reg.register_download_client(3, qbt, priority=50)
        reg.register_download_client(4, tr, priority=5)
        reg.register_download_client(5, dl, priority=30)

        assert reg.get_nzb_client() is nzb  # priority 10
        assert reg.get_torrent_client() is tr  # priority 5
        assert reg.get_client_for_type("deluge") is dl


class TestBackwardCompatibility:
    """Existing code that doesn't pass priority still works."""

    def test_register_without_priority_uses_default(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, sab)

        assert reg.get_nzb_client() is sab

    def test_get_download_clients_returns_all(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        qbt = _mock_client("qbittorrent")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, qbt, priority=50)

        clients = reg.get_download_clients()
        assert len(clients) == 2

    def test_get_download_client_items_returns_all(self) -> None:
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, sab, priority=50)

        items = reg.get_download_client_items()
        assert len(items) == 1
        assert items[0][0] == 1
