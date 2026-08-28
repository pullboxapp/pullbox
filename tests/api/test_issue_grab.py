"""Tests for the grab-specific-release endpoint.

Verifies:
- POST /api/v1/issues/{id}/grab creates DownloadHistory and returns 201
- Issue status updates to DOWNLOADING after grab
- Torrent releases route to qBittorrent client
- NZB releases route to SABnzbd client
- Nonexistent issue returns 404
- Manual grab is logged distinctly from automated grab
- No download clients returns error
- grab_release() method constructs correct ReleaseResult

Run:
    pytest tests/api/test_issue_grab.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.indexer import IndexerConfig, IndexerSource, IndexerType
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-grab-release")


# ── Test Data ──────────────────────────────────────────────────────────


GRAB_BODY_NZB = {
    "download_url": "https://api.nzbgeek.info/api?t=get&id=abc123",
    "title": "Batman 001 (2016) [Digital].cbz",
    "indexer_name": "NZBgeek",
    "is_torrent": False,
    "file_size": 52_428_800,
}

GRAB_BODY_TORRENT = {
    "download_url": "magnet:?xt=urn:btih:abc123&dn=Batman+001",
    "title": "Batman 001 (2016) [Digital].cbz",
    "indexer_name": "Torznab",
    "is_torrent": True,
    "file_size": 52_428_800,
}


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "f" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="grabuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="grab-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _create_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    year_start: int = 2016,
    issue_number: float = 1.0,
) -> int:
    """Seed a series + issue, return the issue_id."""
    async with factory() as session:
        series = Series(
            comicvine_id=99900,
            title=series_title,
            sort_title=series_title,
            year_start=year_start,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=issue_number,
            title=f"Issue #{int(issue_number)}",
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.commit()
        return issue.id


async def _create_owned_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    library_path: str = "/tmp/test-comics/Batman/Batman 001.cbz",
) -> int:
    """Seed an owned issue with a library file, return the issue id."""
    async with factory() as session:
        root = LibraryRoot(name="Comics", path="/tmp/test-comics", enabled=True)
        session.add(root)
        await session.flush()

        series = Series(
            comicvine_id=99901,
            title="Batman",
            sort_title="batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50002,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()

        from datetime import UTC, datetime

        session.add(
            LibraryFile(
                file_path=library_path,
                file_name="Batman 001.cbz",
                file_size=123,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue_id=issue.id,
                library_root_id=root.id,
            )
        )
        await session.commit()
        return issue.id


def _mock_nzb_client() -> AsyncMock:
    """Create a mock NZB client (SABnzbd)."""
    client = AsyncMock()
    client.name = "SABnzbd"
    client.client_type = "sabnzbd"
    client.add_nzb = AsyncMock(return_value="sab-ext-id-123")
    return client


def _mock_torrent_client() -> AsyncMock:
    """Create a mock torrent client (qBittorrent)."""
    client = AsyncMock()
    client.name = "qBittorrent"
    client.client_type = "qbittorrent"
    client.add_torrent = AsyncMock(return_value="qbt-hash-abc")
    client.add_torrent_data = AsyncMock(return_value="qbt-hash-data")
    return client


def _mock_registry(*, nzb: AsyncMock | None = None, torrent: AsyncMock | None = None) -> AsyncMock:
    """Create a mock ProviderRegistry with optional clients."""
    registry = AsyncMock()
    registry.get_nzb_client = lambda: nzb
    registry.get_torrent_client = lambda: torrent
    registry.get_download_client_items = lambda: []
    registry.get_indexer = lambda _config_id: None
    return registry


# ── Unit Tests: grab_release() ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_grab_release_constructs_release_result(
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """grab_release() constructs a ReleaseResult and delegates to send_to_client()."""
    from pullbox.core.events import EventBus
    from pullbox.services.download_service import DownloadService

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)
    event_bus = EventBus()
    svc = DownloadService(registry=registry, event_bus=event_bus)

    async with _db_factory() as session:
        issue_id = await _create_issue(_db_factory)

        download = await svc.grab_release(
            session,
            issue_id=issue_id,
            download_url="https://example.com/nzb/123",
            title="Batman 001 (2016).cbz",
            indexer_name="NZBgeek",
            is_torrent=False,
            file_size=50_000_000,
        )
        await session.commit()

    assert download.issue_id == issue_id
    assert download.title == "Batman 001 (2016).cbz"
    assert download.file_size == 50_000_000
    nzb_client.add_nzb.assert_called_once()


@pytest.mark.asyncio
async def test_grab_resolver_enabled_torznab_fetches_descriptor_inside_pullbox(
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A challenged Torznab descriptor is uploaded as bytes, not leaked to the client."""
    from pullbox.core.events import EventBus
    from pullbox.providers.indexer.torznab_transport import TorznabDescriptor
    from pullbox.services.download_service import DownloadService

    torrent_client = _mock_torrent_client()
    indexer = AsyncMock()
    indexer.browser_resolver_enabled = True
    indexer.fetch_torrent_descriptor = AsyncMock(
        return_value=TorznabDescriptor(content=b"torrent-bytes", magnet_url=None)
    )
    registry = _mock_registry(torrent=torrent_client)
    registry.get_indexer = lambda config_id: indexer if config_id == 42 else None
    svc = DownloadService(registry=registry, event_bus=EventBus())

    async with _db_factory() as session:
        issue_id = await _create_issue(_db_factory)
        session.add(
            IndexerConfig(
                id=42,
                name="Manual Torznab",
                indexer_type=IndexerType.TORZNAB,
                source=IndexerSource.MANUAL,
                url="https://indexer.example",
                api_key="encrypted",
                enabled=True,
                resolver_enabled=True,
            )
        )
        await session.flush()
        download = await svc.grab_release(
            session,
            issue_id=issue_id,
            download_url="https://indexer.example/api?t=get&id=7&apikey=secret",
            title="Ubuntu fixture",
            indexer_name="Manual Torznab",
            indexer_id=42,
            is_torrent=True,
        )

    assert download.indexer_id == 42
    indexer.fetch_torrent_descriptor.assert_awaited_once()
    torrent_client.add_torrent_data.assert_awaited_once_with(b"torrent-bytes", "Ubuntu fixture")
    torrent_client.add_torrent.assert_not_awaited()
    from pullbox.tasks.download_progress import clear_download_progress

    clear_download_progress(download.id)


# ── API Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grab_creates_download_history(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /issues/{id}/grab creates DownloadHistory and returns 201."""
    issue_id = await _create_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201
    data = resp.json()
    assert data["issue_id"] == issue_id
    assert data["download_id"] > 0
    assert data["title"] == GRAB_BODY_NZB["title"]
    assert data["status"] == "sent"


@pytest.mark.asyncio
async def test_grab_owned_issue_marks_download_as_replacement(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Find alternative grabs for owned issues persist replacement intent."""
    issue_id = await _create_owned_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201

    async with _db_factory() as session:
        downloads = (
            (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.issue_id == issue_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(downloads) == 1
    assert downloads[0].replace_existing_file is True


@pytest.mark.asyncio
async def test_grab_updates_issue_status(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After grab, issue status changes to DOWNLOADING."""
    issue_id = await _create_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201

    # Verify issue status was updated
    async with _db_factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_grab_with_torrent(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Torrent releases are sent to qBittorrent client."""
    issue_id = await _create_issue(_db_factory)

    torrent_client = _mock_torrent_client()
    registry = _mock_registry(torrent=torrent_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_TORRENT)

    assert resp.status_code == 201
    torrent_client.add_torrent.assert_called_once()


@pytest.mark.asyncio
async def test_grab_rejects_unknown_originating_indexer(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale or forged result cannot create a download with an invalid FK."""
    issue_id = await _create_issue(_db_factory)
    torrent_client = _mock_torrent_client()
    registry = _mock_registry(torrent=torrent_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/grab",
            json={**GRAB_BODY_TORRENT, "indexer_id": 999_999},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == (
        "The originating indexer is no longer available. Run the search again."
    )
    torrent_client.add_torrent.assert_not_awaited()


@pytest.mark.asyncio
async def test_grab_with_nzb(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NZB releases are sent to SABnzbd client."""
    issue_id = await _create_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201
    nzb_client.add_nzb.assert_called_once()


@pytest.mark.asyncio
async def test_grab_invalid_issue_returns_404(client: AsyncClient) -> None:
    """POST /issues/99999/grab returns 404."""
    resp = await client.post("/api/v1/issues/99999/grab", json=GRAB_BODY_NZB)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_grab_logs_manual_action(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Manual grab is logged distinctly with manual=True indicator."""
    issue_id = await _create_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201

    # structlog renders to Python logging; caplog captures the rendered dicts
    manual_logs = [r for r in caplog.records if "issue_manual_grab" in r.getMessage()]
    assert len(manual_logs) >= 1
    assert "'manual': True" in manual_logs[0].getMessage()


@pytest.mark.asyncio
async def test_grab_no_clients_returns_error(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No configured download clients returns an error."""
    issue_id = await _create_issue(_db_factory)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    # ProviderError maps to 502 or similar error status
    assert resp.status_code >= 400


@pytest.mark.asyncio
async def test_grab_response_schema(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Response matches GrabReleaseResponse schema."""
    issue_id = await _create_issue(_db_factory)

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/grab", json=GRAB_BODY_NZB)

    assert resp.status_code == 201
    data = resp.json()
    assert set(data.keys()) == {"issue_id", "download_id", "title", "status"}
    assert isinstance(data["issue_id"], int)
    assert isinstance(data["download_id"], int)
    assert isinstance(data["title"], str)
    assert isinstance(data["status"], str)


@pytest.mark.asyncio
async def test_grab_updates_originating_search_log_counts(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manual grab increments only the grabbed count on the originating search log."""
    issue_id = await _create_issue(_db_factory)

    async with _db_factory() as session:
        search_log = SearchLog(
            issue_id=issue_id,
            series_title="Batman",
            issue_number=1.0,
            search_type=SearchType.MANUAL,
            results_found=8,
            results_grabbed=0,
            results_queued=2,
            results_rejected=3,
        )
        session.add(search_log)
        await session.commit()
        search_log_id = search_log.id

    nzb_client = _mock_nzb_client()
    registry = _mock_registry(nzb=nzb_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/grab",
            json={**GRAB_BODY_NZB, "search_log_id": search_log_id},
        )

    assert resp.status_code == 201

    async with _db_factory() as session:
        updated_log = await session.get(SearchLog, search_log_id)

    assert updated_log is not None
    assert updated_log.results_grabbed == 1
    assert updated_log.results_queued == 2
    assert updated_log.results_rejected == 3


@pytest.mark.asyncio
async def test_grab_failed_send_returns_error_and_does_not_increment_search_log(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Immediate client rejection is a failed grab, not a successful manual grab."""
    issue_id = await _create_issue(_db_factory)

    async with _db_factory() as session:
        search_log = SearchLog(
            issue_id=issue_id,
            series_title="Batman",
            issue_number=1.0,
            search_type=SearchType.MANUAL,
            results_found=1,
            results_grabbed=0,
            results_queued=0,
            results_rejected=1,
        )
        session.add(search_log)
        await session.commit()
        search_log_id = search_log.id

    torrent_client = _mock_torrent_client()
    torrent_client.add_torrent.side_effect = RuntimeError("qBittorrent did not add torrent")
    registry = _mock_registry(torrent=torrent_client)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=(registry, {}),
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/grab",
            json={**GRAB_BODY_TORRENT, "search_log_id": search_log_id},
        )

    assert resp.status_code == 502
    assert "qBittorrent did not add torrent" in resp.json()["detail"]

    async with _db_factory() as session:
        updated_log = await session.get(SearchLog, search_log_id)
        downloads = (
            (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.issue_id == issue_id)
                )
            )
            .scalars()
            .all()
        )

    assert updated_log is not None
    assert updated_log.results_grabbed == 0
    assert len(downloads) == 1
    assert downloads[0].state == DownloadState.FAILED
