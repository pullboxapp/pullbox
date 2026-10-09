"""Tests for the clear download history endpoint (C-2.4).

Covers:
- Bulk deletion of download-page records (download-only COMPLETED, download-only FAILED)
- Preservation of active downloads (QUEUED, SENT, DOWNLOADING, PAUSED, etc.)
- Preservation of imported/post-processing-complete records
- Preservation of failed post-processing records
- Empty history edge case
- Mixed states
- Authentication requirement
- Response format
- Route ordering (DELETE /history vs DELETE /{download_id})
- Idempotency

Run:
    pytest tests/api/test_clear_download_history.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.download import (
    DownloadClientType,
    DownloadHistory,
    DownloadState,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-clear-history")

CLEAR_URL = "/api/v1/downloads/history"


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
    db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with db_factory() as session:
        user = User(
            username="clearuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="clear-test"))
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


@pytest.fixture
async def unauthed_client(
    db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with no authentication."""
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
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Helpers ───────────────────────────────────────────────────────────


async def _ensure_issue(factory: async_sessionmaker[AsyncSession]) -> int:
    """Create a Series + Issue, return the issue ID."""
    async with factory() as session:
        series = Series(title="Test Series", sort_title="test series")
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=1.0)
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
        return issue_id


async def _seed_downloads(
    factory: async_sessionmaker[AsyncSession],
    states: list[str],
    issue_id: int,
    *,
    downloaded_path: str | None = None,
) -> list[int]:
    """Insert DownloadHistory rows with the given states, return their IDs.

    If downloaded_path is provided it is set on all rows. Otherwise,
    rows with state 'imported' or 'post_processing' get a default
    downloaded_path (simulating a successfully-downloaded file).
    """
    ids: list[int] = []
    async with factory() as session:
        for i, state_str in enumerate(states):
            # Post-processing-related states always have a downloaded_path
            dl_path = downloaded_path
            if dl_path is None and state_str in ("imported", "post_processing"):
                dl_path = f"/data/comics/release-{i}.cbz"
            dl = DownloadHistory(
                title=f"test-release-{i}",
                state=DownloadState(state_str),
                download_client=DownloadClientType.SABNZBD,
                download_url=f"https://example.com/release-{i}.nzb",
                external_id=f"ext-{i}",
                issue_id=issue_id,
                downloaded_path=dl_path,
            )
            session.add(dl)
            await session.flush()
            ids.append(dl.id)
        await session.commit()
    return ids


async def _count_downloads(factory: async_sessionmaker[AsyncSession]) -> int:
    """Count all DownloadHistory rows."""
    async with factory() as session:
        result = await session.execute(select(func.count(DownloadHistory.id)))
        return result.scalar_one()


# ── Tests ─────────────────────────────────────────────────────────────


class TestClearDownloadHistory:
    """DELETE /api/v1/downloads/history — bulk clear terminal records."""

    @pytest.mark.asyncio
    async def test_queue_serializes_issue_series_without_lazy_load(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /downloads/queue includes series context without async lazy-load errors."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["queued"], issue_id)

        resp = await client.get("/api/v1/downloads/queue")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["series_title"] == "Test Series"
        assert body[0]["issue_number"] == 1.0

    @pytest.mark.asyncio
    async def test_history_serializes_issue_series_without_lazy_load(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /downloads/history includes series context without async lazy-load errors."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed"], issue_id)

        resp = await client.get("/api/v1/downloads/history")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["series_title"] == "Test Series"
        assert body["items"][0]["issue_number"] == 1.0

    @pytest.mark.asyncio
    async def test_clears_completed_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Completed downloads are cleared (terminal state on downloads page)."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed", "completed"], issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_clears_failed_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Failed downloads are deleted."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["failed", "failed", "failed"], issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 3

    @pytest.mark.asyncio
    async def test_preserves_imported_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Imported downloads are preserved (belong on post-processing page)."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["imported"], issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert await _count_downloads(db_factory) == 1

    @pytest.mark.asyncio
    async def test_preserves_active_downloads(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Active states (queued, sent, downloading, paused) are NOT deleted."""
        issue_id = await _ensure_issue(db_factory)
        active = ["queued", "sent", "downloading", "paused"]
        await _seed_downloads(db_factory, active, issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert await _count_downloads(db_factory) == 4

    @pytest.mark.asyncio
    async def test_preserves_retry_pending_and_post_processing(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """RETRY_PENDING and POST_PROCESSING are also preserved."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["retry_pending", "post_processing"], issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert await _count_downloads(db_factory) == 2

    @pytest.mark.asyncio
    async def test_mixed_states_only_deletes_download_page_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only completed/failed history is removed; active/imported survive."""
        issue_id = await _ensure_issue(db_factory)
        # completed + failed = download history records
        # imported = post-processing page record
        # active states = active downloads
        states = [
            "queued",
            "completed",
            "downloading",
            "failed",
            "imported",
            "sent",
            "retry_pending",
        ]
        await _seed_downloads(db_factory, states, issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2  # completed + failed
        assert await _count_downloads(db_factory) == 5

    @pytest.mark.asyncio
    async def test_empty_history_returns_zero(
        self,
        client: AsyncClient,
    ) -> None:
        """No records → deleted count is 0."""
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 0}

    @pytest.mark.asyncio
    async def test_response_format(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Response is JSON with a single 'deleted' key containing an integer."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["failed"], issue_id)
        resp = await client.delete(CLEAR_URL)
        body = resp.json()
        assert set(body.keys()) == {"deleted"}
        assert isinstance(body["deleted"], int)

    @pytest.mark.asyncio
    async def test_idempotent_second_call(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Calling twice: second call returns 0 deleted."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed", "failed"], issue_id)
        resp1 = await client.delete(CLEAR_URL)
        assert resp1.json()["deleted"] == 2

        resp2 = await client.delete(CLEAR_URL)
        assert resp2.json()["deleted"] == 0

    @pytest.mark.asyncio
    async def test_every_non_clearable_state_survives(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """States not shown on downloads page are preserved by clear."""
        issue_id = await _ensure_issue(db_factory)
        # Active downloads + post-processing + imported survive clear
        non_clearable = [
            "queued",
            "sent",
            "downloading",
            "paused",
            "retry_pending",
            "post_processing",
            "imported",
        ]
        await _seed_downloads(db_factory, non_clearable, issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.json()["deleted"] == 0
        assert await _count_downloads(db_factory) == len(non_clearable)

    @pytest.mark.asyncio
    async def test_download_page_states_cleared_pp_preserved(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """COMPLETED + FAILED are cleared; IMPORTED is preserved."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed", "failed", "imported"], issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2  # completed + failed
        assert await _count_downloads(db_factory) == 1  # imported survives

    @pytest.mark.asyncio
    async def test_large_batch_deletion(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Handles bulk deletion of many records."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed"] * 50, issue_id)
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 50

    @pytest.mark.asyncio
    async def test_failed_post_processing_records_are_preserved(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """FAILED post-processing rows stay out of download-history clears."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(
            db_factory,
            ["failed"],
            issue_id,
            downloaded_path="/data/comics/test.cbz",
        )
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert await _count_downloads(db_factory) == 1

    @pytest.mark.asyncio
    async def test_mixed_failed_rows_clear_only_download_failures(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Plain failed rows clear; post-processing failures remain."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed", "failed"], issue_id)
        await _seed_downloads(
            db_factory,
            ["failed", "imported"],
            issue_id,
            downloaded_path="/data/comics/test.cbz",
        )

        resp = await client.delete(CLEAR_URL)

        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2
        assert await _count_downloads(db_factory) == 2

    @pytest.mark.asyncio
    async def test_cancelled_by_user_cleared(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """FAILED with error_message 'Cancelled by user' is cleared."""
        issue_id = await _ensure_issue(db_factory)
        async with db_factory() as session:
            dl = DownloadHistory(
                title="cancelled-release",
                state=DownloadState.FAILED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/cancelled.nzb",
                external_id="ext-cancelled",
                issue_id=issue_id,
                error_message="Cancelled by user",
            )
            session.add(dl)
            await session.commit()
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

    @pytest.mark.asyncio
    async def test_large_batch_1000_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Handles bulk deletion of 1000+ records (C-2.4 edge case)."""
        issue_id = await _ensure_issue(db_factory)
        # Seed in batches to avoid overwhelming single transaction
        batch_size = 200
        for batch_start in range(0, 1000, batch_size):
            async with db_factory() as session:
                for i in range(batch_start, batch_start + batch_size):
                    dl = DownloadHistory(
                        title=f"release-{i}",
                        state=DownloadState.COMPLETED,
                        download_client=DownloadClientType.SABNZBD,
                        download_url=f"https://example.com/r-{i}.nzb",
                        external_id=f"ext-{i}",
                        issue_id=issue_id,
                    )
                    session.add(dl)
                await session.commit()
        resp = await client.delete(CLEAR_URL)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1000
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_concurrent_clear_is_safe(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two rapid clear calls don't error — second returns 0 deleted."""
        issue_id = await _ensure_issue(db_factory)
        await _seed_downloads(db_factory, ["completed"] * 10, issue_id)
        resp1 = await client.delete(CLEAR_URL)
        resp2 = await client.delete(CLEAR_URL)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        total_deleted = resp1.json()["deleted"] + resp2.json()["deleted"]
        assert total_deleted == 10
        assert await _count_downloads(db_factory) == 0


class TestClearDownloadHistoryAuth:
    """Authentication requirements for clear history endpoint."""

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(
        self,
        unauthed_client: AsyncClient,
    ) -> None:
        """Unauthenticated requests are rejected."""
        resp = await unauthed_client.delete(CLEAR_URL)
        assert resp.status_code in (401, 403, 307)


class TestClearVsCancelRouteOrdering:
    """Verify DELETE /history doesn't conflict with DELETE /{download_id}."""

    @pytest.mark.asyncio
    async def test_history_route_not_captured_as_id(
        self,
        client: AsyncClient,
    ) -> None:
        """DELETE /downloads/history hits the clear endpoint, not cancel."""
        resp = await client.delete(CLEAR_URL)
        # If route ordering is wrong, "history" would be parsed as download_id
        # and we'd get 422 (validation error for non-integer) or 404
        assert resp.status_code == 200
        assert "deleted" in resp.json()

    @pytest.mark.asyncio
    async def test_numeric_id_still_works(
        self,
        client: AsyncClient,
    ) -> None:
        """DELETE /downloads/999 still hits the cancel endpoint (returns 404)."""
        resp = await client.delete("/api/v1/downloads/999")
        assert resp.status_code == 404


class TestDeleteProcessingProtection:
    """Downloads in POST_PROCESSING state cannot be deleted."""

    @pytest.mark.asyncio
    async def test_completed_download_can_be_deleted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """COMPLETED downloads can be deleted (terminal state on downloads page)."""
        issue_id = await _ensure_issue(db_factory)
        ids = await _seed_downloads(db_factory, ["completed"], issue_id)
        resp = await client.delete(f"/api/v1/downloads/{ids[0]}")
        assert resp.status_code == 204
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_post_processing_download_can_be_deleted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST_PROCESSING downloads can be deleted (terminal state)."""
        issue_id = await _ensure_issue(db_factory)
        ids = await _seed_downloads(db_factory, ["post_processing"], issue_id)
        resp = await client.delete(f"/api/v1/downloads/{ids[0]}")
        assert resp.status_code == 204
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_imported_download_can_be_deleted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """IMPORTED downloads can be deleted (terminal state)."""
        issue_id = await _ensure_issue(db_factory)
        ids = await _seed_downloads(db_factory, ["imported"], issue_id)
        resp = await client.delete(f"/api/v1/downloads/{ids[0]}")
        assert resp.status_code == 204
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_failed_download_can_be_deleted(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """FAILED downloads can be deleted (terminal state)."""
        issue_id = await _ensure_issue(db_factory)
        ids = await _seed_downloads(db_factory, ["failed"], issue_id)
        resp = await client.delete(f"/api/v1/downloads/{ids[0]}")
        assert resp.status_code == 204
        assert await _count_downloads(db_factory) == 0
