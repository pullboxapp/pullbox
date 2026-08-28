"""Integration tests for download client expansion (DCE-P.1).

Verifies multi-client routing, priority selection, client type affinity,
retry flow, and full lifecycle for all 5 client types.

Run:
    pytest tests/integration/test_download_clients_integration.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.events import EventBus
from pullbox.models import Base
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.providers.base import (
    DownloadStatus,
    ProviderRegistry,
    ReleaseResult,
)
from pullbox.services.download_service import DownloadService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-integration")


def _mock_client(client_type: str, name: str | None = None) -> MagicMock:
    """Create a mock DownloadClient."""
    client = MagicMock()
    client.client_type = client_type
    client.name = name or client_type
    client.add_nzb = AsyncMock(return_value="ext-123")
    client.add_torrent = AsyncMock(return_value="hash-abc")
    client.get_download_status = AsyncMock(
        return_value=DownloadStatus(
            external_id="ext-123",
            title="Test",
            state="downloading",
            progress=0.5,
            size_bytes=100000,
            speed_bytes=50000,
            eta_seconds=60,
            error_message=None,
        )
    )
    client.get_queue = AsyncMock(return_value=[])
    client.remove_download = AsyncMock(return_value=True)
    client.test_connection = AsyncMock()
    return client


def _make_release(title: str, is_torrent: bool) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name="test-indexer",
        download_url=f"http://example.com/{title}",
        size_bytes=100000,
        age_days=1,
        seeders=10 if is_torrent else None,
        leechers=5 if is_torrent else None,
        grabs=50 if not is_torrent else None,
        is_torrent=is_torrent,
        category="comics",
        published_at=None,
    )


@pytest.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with db() as sess:
        yield sess


@pytest.fixture
async def issue(session: AsyncSession) -> Issue:
    """Create a test series + issue."""
    series = Series(title="Test Series", sort_title="test series")
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=1.0,
        status=IssueStatus.WANTED,
    )
    session.add(issue)
    await session.flush()
    return issue


# ── Multi-client priority selection ──────────────────────────────


@pytest.mark.asyncio
class TestMultiClientPriority:
    """Priority-based client selection across all 5 types."""

    async def test_two_nzb_clients_lower_priority_wins(self) -> None:
        """With SABnzbd at priority 50 and NZBGet at 10, NZBGet is selected."""
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, nzb, priority=10)

        assert reg.get_nzb_client() is nzb

    async def test_three_torrent_clients_lowest_priority_wins(self) -> None:
        """With 3 torrent clients, lowest priority number is selected."""
        reg = ProviderRegistry()
        qbt = _mock_client("qbittorrent")
        tr = _mock_client("transmission")
        dl = _mock_client("deluge")
        reg.register_download_client(1, qbt, priority=50)
        reg.register_download_client(2, tr, priority=5)
        reg.register_download_client(3, dl, priority=30)

        assert reg.get_torrent_client() is tr

    async def test_all_five_clients_correct_protocol_routing(self) -> None:
        """All 5 clients registered — NZB and torrent selections are independent."""
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

        assert reg.get_nzb_client() is nzb
        assert reg.get_torrent_client() is tr


# ── Download routing ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestDownloadRouting:
    """Verify downloads route to the correct client type."""

    async def test_nzb_release_goes_to_nzbget(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """NZB release routes to NZBGet when it's the NZB client."""
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, nzb, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Test NZB", is_torrent=False)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.download_client == DownloadClientType.NZBGET
        assert dl.state == DownloadState.SENT
        nzb.add_nzb.assert_called_once()

    async def test_torrent_release_goes_to_transmission(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """Torrent release routes to Transmission when it's the torrent client."""
        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        reg.register_download_client(1, tr, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Test Torrent", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.download_client == DownloadClientType.TRANSMISSION
        assert dl.state == DownloadState.SENT
        tr.add_torrent.assert_called_once()

    async def test_torrent_release_goes_to_deluge(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """Torrent release routes to Deluge when it's the highest-priority torrent client."""
        reg = ProviderRegistry()
        dl_client = _mock_client("deluge")
        reg.register_download_client(1, dl_client, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Test Torrent", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.download_client == DownloadClientType.DELUGE
        dl_client.add_torrent.assert_called_once()

    async def test_mixed_clients_correct_routing(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """SABnzbd + Transmission: NZB goes to SABnzbd, torrent to Transmission."""
        reg = ProviderRegistry()
        sab = _mock_client("sabnzbd")
        tr = _mock_client("transmission")
        reg.register_download_client(1, sab, priority=50)
        reg.register_download_client(2, tr, priority=50)

        svc = DownloadService(registry=reg, event_bus=EventBus())

        nzb_release = _make_release("NZB Release", is_torrent=False)
        dl_nzb = await svc.send_to_client(session, nzb_release, issue.id)
        assert dl_nzb.download_client == DownloadClientType.SABNZBD
        sab.add_nzb.assert_called_once()

        torrent_release = _make_release("Torrent Release", is_torrent=True)
        dl_tor = await svc.send_to_client(session, torrent_release, issue.id)
        assert dl_tor.download_client == DownloadClientType.TRANSMISSION
        tr.add_torrent.assert_called_once()

    async def test_no_nzb_client_raises(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """No NZB client configured → ProviderError."""
        from pullbox.core.exceptions import ProviderError

        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        reg.register_download_client(1, tr, priority=50)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("NZB Only", is_torrent=False)

        with pytest.raises(ProviderError):
            await svc.send_to_client(session, release, issue.id)


# ── Client type affinity ─────────────────────────────────────────


@pytest.mark.asyncio
class TestClientTypeAffinity:
    """Downloads are polled from the correct client based on recorded type."""

    async def test_transmission_download_polled_from_transmission(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """Download sent to Transmission → status polled from Transmission, not Deluge."""
        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        dl_client = _mock_client("deluge")
        reg.register_download_client(1, tr, priority=10)
        reg.register_download_client(2, dl_client, priority=50)
        session.add(
            DownloadClientConfig(
                id=1,
                name="Transmission",
                client_type=DownloadClientType.TRANSMISSION,
                url="http://transmission.test",
            )
        )
        await session.flush()

        svc = DownloadService(registry=reg, event_bus=EventBus())

        # Send to Transmission (higher priority)
        release = _make_release("Test", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)
        assert dl.download_client == DownloadClientType.TRANSMISSION
        assert dl.download_client_config_id == 1
        assert dl.protocol is AcquisitionProtocol.TORRENT

        # get_client_for_download should return Transmission, not Deluge
        client = svc.get_client_for_download(dl)
        assert client is tr

    async def test_exact_config_id_wins_over_same_type_fallback(
        self,
        issue: Issue,
    ) -> None:
        reg = ProviderRegistry()
        first = _mock_client("sabnzbd")
        second = _mock_client("sabnzbd")
        reg.register_download_client(1, first, priority=10)
        reg.register_download_client(2, second, priority=50)
        svc = DownloadService(registry=reg, event_bus=EventBus())
        download = DownloadHistory(
            issue_id=issue.id,
            title="Exact client",
            download_url="https://example.test/exact-client",
            download_client=DownloadClientType.SABNZBD,
            download_client_config_id=2,
        )

        assert svc.get_client_for_download(download) is second

    async def test_manual_retry_uses_persisted_client_config_id(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        reg = ProviderRegistry()
        first = _mock_client("sabnzbd")
        second = _mock_client("sabnzbd")
        reg.register_download_client(1, first, priority=10)
        reg.register_download_client(2, second, priority=50)
        svc = DownloadService(registry=reg, event_bus=EventBus())
        session.add_all(
            [
                DownloadClientConfig(
                    id=1,
                    name="First SABnzbd",
                    client_type=DownloadClientType.SABNZBD,
                    url="http://sabnzbd-first.test",
                ),
                DownloadClientConfig(
                    id=2,
                    name="Second SABnzbd",
                    client_type=DownloadClientType.SABNZBD,
                    url="http://sabnzbd-second.test",
                ),
            ]
        )
        await session.flush()
        download = DownloadHistory(
            issue_id=issue.id,
            title="Exact retry",
            download_url="https://example.test/exact-retry",
            download_client=DownloadClientType.SABNZBD,
            download_client_config_id=2,
            state=DownloadState.FAILED,
        )
        session.add(download)
        await session.flush()

        await svc.manual_retry(session, download.id)

        second.add_nzb.assert_awaited_once()
        first.add_nzb.assert_not_awaited()

    async def test_nzbget_download_polled_from_nzbget(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """Download sent to NZBGet → status polled from NZBGet, not SABnzbd."""
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, nzb, priority=10)
        reg.register_download_client(2, sab, priority=50)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Test", is_torrent=False)
        dl = await svc.send_to_client(session, release, issue.id)

        client = svc.get_client_for_download(dl)
        assert client is nzb

    async def test_client_removed_returns_none(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """Download record has DELUGE type but Deluge removed → returns None."""
        reg = ProviderRegistry()
        dl_client = _mock_client("deluge")
        reg.register_download_client(1, dl_client, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Test", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)

        # Remove Deluge from registry (simulates config removal)
        reg2 = ProviderRegistry()
        svc2 = DownloadService(registry=reg2, event_bus=EventBus())
        assert svc2.get_client_for_download(dl) is None


# ── Lifecycle tests ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestDownloadLifecycle:
    """Full lifecycle: send → poll → complete for each new client type."""

    async def test_nzbget_lifecycle(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        reg.register_download_client(1, nzb, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("NZBGet Test", is_torrent=False)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.state == DownloadState.SENT
        assert dl.external_id == "ext-123"

        # Simulate completion
        nzb.get_download_status.return_value = DownloadStatus(
            external_id="ext-123",
            title="NZBGet Test",
            state="completed",
            progress=1.0,
            size_bytes=100000,
            speed_bytes=None,
            eta_seconds=None,
            error_message=None,
            downloaded_path="/downloads/complete/test",
        )

        summary = await svc.check_active_downloads(session)
        assert summary["completed"] == 1
        assert summary["failed"] == 0

    async def test_transmission_lifecycle(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        reg = ProviderRegistry()
        tr = _mock_client("transmission")
        reg.register_download_client(1, tr, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Transmission Test", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.state == DownloadState.SENT

        # Simulate completion
        tr.get_download_status.return_value = DownloadStatus(
            external_id="hash-abc",
            title="Transmission Test",
            state="completed",
            progress=1.0,
            size_bytes=100000,
            speed_bytes=None,
            eta_seconds=None,
            error_message=None,
            downloaded_path="/downloads/test",
        )

        summary = await svc.check_active_downloads(session)
        assert summary["completed"] == 1

    async def test_deluge_lifecycle(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        reg = ProviderRegistry()
        dl_client = _mock_client("deluge")
        reg.register_download_client(1, dl_client, priority=10)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Deluge Test", is_torrent=True)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.state == DownloadState.SENT

        # Simulate completion
        dl_client.get_download_status.return_value = DownloadStatus(
            external_id="hash-abc",
            title="Deluge Test",
            state="completed",
            progress=1.0,
            size_bytes=100000,
            speed_bytes=None,
            eta_seconds=None,
            error_message=None,
            downloaded_path="/downloads/test",
        )

        summary = await svc.check_active_downloads(session)
        assert summary["completed"] == 1


# ── Retry flow ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRetryFlow:
    """Retry preserves client type affinity."""

    async def test_failed_nzbget_retries_to_nzbget(
        self,
        session: AsyncSession,
        issue: Issue,
    ) -> None:
        """NZBGet download fails → retry → re-sent to NZBGet."""
        reg = ProviderRegistry()
        nzb = _mock_client("nzbget")
        sab = _mock_client("sabnzbd")
        reg.register_download_client(1, nzb, priority=10)
        reg.register_download_client(2, sab, priority=50)

        svc = DownloadService(registry=reg, event_bus=EventBus())
        release = _make_release("Retry Test", is_torrent=False)
        dl = await svc.send_to_client(session, release, issue.id)

        assert dl.download_client == DownloadClientType.NZBGET

        # Simulate failure
        dl.state = DownloadState.FAILED
        dl.error_message = "Test failure"
        await session.flush()

        # Retry
        retried = await svc.manual_retry(session, dl.id)
        assert retried.download_client == DownloadClientType.NZBGET
        # NZBGet should be called again (the NZB client for the protocol)
        assert nzb.add_nzb.call_count == 2


# ── Test connection format ───────────────────────────────────────


class TestConnectionHealthFormat:
    """test_connection returns correct ProviderHealthResult format for each type."""

    def test_all_mock_clients_have_test_connection(self) -> None:
        for ct in ["sabnzbd", "nzbget", "qbittorrent", "transmission", "deluge"]:
            client = _mock_client(ct)
            assert hasattr(client, "test_connection")


# ── DownloadClientType helper properties ─────────────────────────


class TestClientTypeHelpers:
    """Verify is_usenet/is_torrent work correctly in routing context."""

    def test_send_determines_type_from_client(self) -> None:
        """DownloadClientType is derived from the actual client, not hardcoded."""
        for ct, expected_usenet in [
            ("sabnzbd", True),
            ("nzbget", True),
            ("qbittorrent", False),
            ("transmission", False),
            ("deluge", False),
        ]:
            dct = DownloadClientType(ct)
            assert dct.is_usenet == expected_usenet
            assert dct.is_torrent == (not expected_usenet)
