"""Tests for clearing post-processing history records."""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault(
    "PULLBOX_SECRET_KEY",
    "test-secret-key-for-clear-post-processing-history",
)

CLEAR_URL = "/api/v1/downloads/history/post-processing"


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
            username="ppclearuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="pp-clear-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    db_factory: async_sessionmaker[AsyncSession],
    api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key."""
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
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


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


async def _seed_download(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    *,
    title: str,
    state: DownloadState,
    downloaded_path: str | None = None,
    imported_at: datetime | None = None,
    error_message: str | None = None,
) -> int:
    """Insert a download row and return its ID."""
    async with factory() as session:
        download = DownloadHistory(
            issue_id=issue_id,
            title=title,
            state=state,
            download_client=DownloadClientType.SABNZBD,
            download_url=f"https://example.com/{title}",
            external_id=f"ext-{title}",
            downloaded_path=downloaded_path,
            imported_at=imported_at,
            error_message=error_message,
        )
        session.add(download)
        await session.flush()
        download_id = download.id
        await session.commit()
        return download_id


async def _count_downloads(factory: async_sessionmaker[AsyncSession]) -> int:
    """Count all download rows."""
    async with factory() as session:
        result = await session.execute(select(func.count(DownloadHistory.id)))
        return result.scalar_one()


class TestClearPostProcessingHistory:
    """DELETE /api/v1/downloads/history/post-processing."""

    @pytest.mark.asyncio
    async def test_clears_imported_and_failed_post_processing_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _ensure_issue(db_factory)
        await _seed_download(
            db_factory,
            issue_id,
            title="imported.cbz",
            state=DownloadState.COMPLETED,
            downloaded_path="/downloads/imported.cbz",
            imported_at=datetime(2026, 4, 7, tzinfo=UTC),
        )
        await _seed_download(
            db_factory,
            issue_id,
            title="pp-failed.cbz",
            state=DownloadState.FAILED,
            downloaded_path="/downloads/pp-failed.cbz",
            error_message="Move failed",
        )

        response = await client.delete(CLEAR_URL)

        assert response.status_code == 200
        assert response.json() == {"deleted": 2}
        assert await _count_downloads(db_factory) == 0

    @pytest.mark.asyncio
    async def test_preserves_active_and_download_history_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _ensure_issue(db_factory)
        await _seed_download(
            db_factory,
            issue_id,
            title="active-processing.cbz",
            state=DownloadState.COMPLETED,
            downloaded_path="/downloads/active-processing.cbz",
        )
        await _seed_download(
            db_factory,
            issue_id,
            title="download-history-completed.cbz",
            state=DownloadState.COMPLETED,
            downloaded_path=None,
        )
        await _seed_download(
            db_factory,
            issue_id,
            title="download-history-failed.cbz",
            state=DownloadState.FAILED,
            downloaded_path=None,
            error_message="Network failure",
        )
        await _seed_download(
            db_factory,
            issue_id,
            title="cancelled.cbz",
            state=DownloadState.FAILED,
            downloaded_path="/downloads/cancelled.cbz",
            error_message="Cancelled by user",
        )
        await _seed_download(
            db_factory,
            issue_id,
            title="imported.cbz",
            state=DownloadState.COMPLETED,
            downloaded_path="/downloads/imported.cbz",
            imported_at=datetime(2026, 4, 7, tzinfo=UTC),
        )

        response = await client.delete(CLEAR_URL)

        assert response.status_code == 200
        assert response.json() == {"deleted": 1}
        assert await _count_downloads(db_factory) == 4

    @pytest.mark.asyncio
    async def test_requires_authentication(
        self,
        unauthed_client: AsyncClient,
    ) -> None:
        response = await unauthed_client.delete(CLEAR_URL)

        assert response.status_code == 401
