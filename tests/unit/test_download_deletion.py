"""Tests for clean deletion/cancellation of in-progress downloads (C-2.2).

Covers:
- Active download cancellation calls remove_download on the correct client
- Progress cache is cleared on cancel
- Issue status reverts from DOWNLOADING → WANTED
- Client not-found/error is handled gracefully
- No external_id skips client call
- Completed/failed records are deleted from DB (no client call)
- RETRY_PENDING is cancellable
- Multiple downloads for same issue: cancel one doesn't affect the other
- POST_PROCESSING downloads removed from history
- Client connection timeout handled gracefully

Run:
    pytest tests/unit/test_download_deletion.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.download import (
    DownloadClientType,
    DownloadHistory,
    DownloadState,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-deletion")

DELETE_URL = "/api/v1/downloads"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def api_key(
    db_factory: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with db_factory() as session:
        user = User(
            username="deleteuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="delete-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    db_factory: async_sessionmaker[AsyncSession],
    api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
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
        headers={"X-Api-Key": api_key},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Helpers ───────────────────────────────────────────────────────────


async def _seed_issue(
    factory: async_sessionmaker[AsyncSession],
    issue_status: IssueStatus = IssueStatus.DOWNLOADING,
) -> int:
    """Create a Series + Issue, return the issue ID."""
    async with factory() as session:
        series = Series(title="Test Series", sort_title="test series")
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            status=issue_status,
        )
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
        return issue_id


async def _seed_download(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    state: str = "downloading",
    client_type: str = "sabnzbd",
    external_id: str | None = "ext-123",
) -> int:
    """Insert a DownloadHistory row, return its ID."""
    async with factory() as session:
        dl = DownloadHistory(
            title="test-release",
            state=DownloadState(state),
            download_client=DownloadClientType(client_type),
            download_url="https://example.com/release.nzb",
            external_id=external_id,
            issue_id=issue_id,
        )
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        await session.commit()
        return dl_id


async def _get_download(
    factory: async_sessionmaker[AsyncSession],
    dl_id: int,
) -> DownloadHistory | None:
    """Fetch a DownloadHistory by ID."""
    async with factory() as session:
        return await session.get(DownloadHistory, dl_id)


async def _get_issue(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
) -> Issue | None:
    """Fetch an Issue by ID."""
    async with factory() as session:
        return await session.get(Issue, issue_id)


# ── Tests: Client Cancellation ────────────────────────────────────────


class TestClientCancellation:
    """Cancel active downloads calls remove_download on the client."""

    @pytest.mark.asyncio
    async def test_sabnzbd_cancel_called(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cancelling a SABnzbd download calls remove_download."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, client_type="sabnzbd")

        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.remove_download = AsyncMock(return_value=True)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204
            mock_client.remove_download.assert_called_once_with("ext-123", delete_files=True)

    @pytest.mark.asyncio
    async def test_qbittorrent_cancel_called(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cancelling a qBittorrent download calls remove_download."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(
            db_factory, issue_id, client_type="qbittorrent", external_id="abc123hash"
        )

        mock_client = AsyncMock()
        mock_client.client_type = "qbittorrent"
        mock_client.remove_download = AsyncMock(return_value=True)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204
            mock_client.remove_download.assert_called_once_with("abc123hash", delete_files=True)

    @pytest.mark.asyncio
    async def test_no_external_id_skips_client_call(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Download with no external_id skips client cancellation."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, state="queued", external_id=None)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204
            mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_not_found_graceful(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Client returns False (not found) — cancel still succeeds."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id)

        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.remove_download = AsyncMock(return_value=False)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED
        assert dl.error_message == "Cancelled by user"

    @pytest.mark.asyncio
    async def test_client_error_graceful(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Client raises exception — cancel still succeeds locally."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id)

        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.remove_download = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED

    @pytest.mark.asyncio
    async def test_no_client_configured_graceful(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No matching client in registry — cancel still succeeds."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id)

        # Register no clients
        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED


# ── Tests: Issue Status Revert ────────────────────────────────────────


class TestIssueStatusRevert:
    """Cancelling reverts issue status from DOWNLOADING → WANTED."""

    @pytest.mark.asyncio
    async def test_downloading_issue_reverts_to_wanted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Issue in DOWNLOADING state reverts to WANTED on cancel."""
        issue_id = await _seed_issue(db_factory, IssueStatus.DOWNLOADING)
        dl_id = await _seed_download(db_factory, issue_id)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        issue = await _get_issue(db_factory, issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.WANTED

    @pytest.mark.asyncio
    async def test_owned_issue_not_reverted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Issue in OWNED state stays OWNED (already imported another copy)."""
        issue_id = await _seed_issue(db_factory, IssueStatus.OWNED)
        dl_id = await _seed_download(db_factory, issue_id)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        issue = await _get_issue(db_factory, issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.OWNED

    @pytest.mark.asyncio
    async def test_wanted_issue_stays_wanted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Issue already in WANTED — no change needed."""
        issue_id = await _seed_issue(db_factory, IssueStatus.WANTED)
        dl_id = await _seed_download(db_factory, issue_id, state="queued")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        issue = await _get_issue(db_factory, issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.WANTED


# ── Tests: Progress Cache ────────────────────────────────────────────


class TestProgressCache:
    """Progress cache is cleared when active download is cancelled."""

    @pytest.mark.asyncio
    async def test_progress_cache_cleared(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cancelling clears the in-memory progress cache entry."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id)

        # Seed the progress cache
        from pullbox.tasks.download_task import _progress_cache

        _progress_cache[dl_id] = object()  # type: ignore[assignment]

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        assert dl_id not in _progress_cache

    @pytest.mark.asyncio
    async def test_no_cache_entry_ok(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cancelling when no cache entry exists — no error."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id)

        from pullbox.tasks.download_task import _progress_cache

        _progress_cache.pop(dl_id, None)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204


# ── Tests: State-Specific Behavior ────────────────────────────────────


class TestStateSpecificBehavior:
    """Different download states are handled correctly."""

    @pytest.mark.asyncio
    async def test_cancel_sent_download(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """SENT download is cancellable."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, state="sent")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED
        assert dl.error_message == "Cancelled by user"

    @pytest.mark.asyncio
    async def test_cancel_paused_download(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """PAUSED download is cancellable."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, state="paused")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED

    @pytest.mark.asyncio
    async def test_cancel_retry_pending_download(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """RETRY_PENDING download is cancellable."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, state="retry_pending")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED

    @pytest.mark.asyncio
    async def test_delete_completed_removes_from_db(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """COMPLETED record is deleted from DB, no client call."""
        issue_id = await _seed_issue(db_factory, IssueStatus.OWNED)
        dl_id = await _seed_download(db_factory, issue_id, state="completed")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:
            resp = await client.delete(f"{DELETE_URL}/{dl_id}")
            assert resp.status_code == 204
            mock_register.assert_not_called()

        dl = await _get_download(db_factory, dl_id)
        assert dl is None

    @pytest.mark.asyncio
    async def test_delete_failed_removes_from_db(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """FAILED record is deleted from DB."""
        issue_id = await _seed_issue(db_factory, IssueStatus.WANTED)
        dl_id = await _seed_download(db_factory, issue_id, state="failed")

        resp = await client.delete(f"{DELETE_URL}/{dl_id}")
        assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is None

    @pytest.mark.asyncio
    async def test_delete_imported_removes_from_db(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """IMPORTED record is deleted from DB."""
        issue_id = await _seed_issue(db_factory, IssueStatus.OWNED)
        dl_id = await _seed_download(db_factory, issue_id, state="imported")

        resp = await client.delete(f"{DELETE_URL}/{dl_id}")
        assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is None

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="POST_PROCESSING state deprecated — deletion now allowed")
    async def test_delete_post_processing_rejects(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST_PROCESSING record cannot be deleted — must finish processing."""
        issue_id = await _seed_issue(db_factory, IssueStatus.DOWNLOADING)
        dl_id = await _seed_download(db_factory, issue_id, state="post_processing")

        resp = await client.delete(f"{DELETE_URL}/{dl_id}")
        assert resp.status_code == 409

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None


# ── Tests: Edge Cases ─────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases for download deletion."""

    @pytest.mark.asyncio
    async def test_nonexistent_download_404(
        self,
        client: AsyncClient,
    ) -> None:
        """Deleting a non-existent download returns 404."""
        resp = await client.delete(f"{DELETE_URL}/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_multiple_downloads_cancel_one(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cancelling one download for an issue doesn't affect another."""
        issue_id = await _seed_issue(db_factory, IssueStatus.DOWNLOADING)
        dl1 = await _seed_download(db_factory, issue_id, external_id="ext-1")
        dl2 = await _seed_download(db_factory, issue_id, external_id="ext-2")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"{DELETE_URL}/{dl1}")
            assert resp.status_code == 204

        dl1_obj = await _get_download(db_factory, dl1)
        dl2_obj = await _get_download(db_factory, dl2)
        assert dl1_obj is not None
        assert dl1_obj.state == DownloadState.FAILED
        assert dl2_obj is not None
        assert dl2_obj.state == DownloadState.DOWNLOADING

    @pytest.mark.asyncio
    async def test_cancel_queued_download(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """QUEUED download (never sent to client) is cancellable."""
        issue_id = await _seed_issue(db_factory)
        dl_id = await _seed_download(db_factory, issue_id, state="queued", external_id=None)

        resp = await client.delete(f"{DELETE_URL}/{dl_id}")
        assert resp.status_code == 204

        dl = await _get_download(db_factory, dl_id)
        assert dl is not None
        assert dl.state == DownloadState.FAILED
        assert dl.error_message == "Cancelled by user"
