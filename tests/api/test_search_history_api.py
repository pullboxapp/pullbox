"""Tests for search history delete and clear endpoints."""

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
from pullbox.models.issue import Issue
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault(
    "PULLBOX_SECRET_KEY",
    "test-secret-key-for-search-history-api",
)


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
    raw_key = "pb_k1_" + "e" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with db_factory() as session:
        user = User(
            username="searchhistoryuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="search-history-test"))
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


async def _ensure_issue(factory: async_sessionmaker[AsyncSession], issue_number: float) -> int:
    """Create a Series + Issue, return the issue ID."""
    async with factory() as session:
        series = Series(title="Batman", sort_title="batman")
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=issue_number)
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
        return issue_id


async def _seed_search_log(
    factory: async_sessionmaker[AsyncSession],
    *,
    issue_id: int,
    search_type: SearchType,
    confidence: str | None,
) -> int:
    """Insert a search log row and return its ID."""
    async with factory() as session:
        log = SearchLog(
            issue_id=issue_id,
            series_title="Batman",
            issue_number=2.0,
            search_type=search_type,
            results_found=12,
            results_grabbed=1,
            results_queued=2,
            results_rejected=4,
            best_confidence=confidence,
            details={"search_time_ms": 184, "search_passes": 2},
            created_at=datetime(2026, 4, 7, tzinfo=UTC),
            updated_at=datetime(2026, 4, 7, tzinfo=UTC),
        )
        session.add(log)
        await session.flush()
        log_id = log.id
        await session.commit()
        return log_id


async def _count_logs(factory: async_sessionmaker[AsyncSession]) -> int:
    """Count all search log rows."""
    async with factory() as session:
        result = await session.execute(select(func.count(SearchLog.id)))
        return result.scalar_one()


class TestSearchHistoryDelete:
    """DELETE /api/v1/search/history/{log_id}."""

    @pytest.mark.asyncio
    async def test_deletes_single_search_log(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _ensure_issue(db_factory, 2.0)
        log_id = await _seed_search_log(
            db_factory,
            issue_id=issue_id,
            search_type=SearchType.MANUAL,
            confidence="high",
        )

        response = await client.delete(f"/api/v1/search/history/{log_id}")

        assert response.status_code == 204
        assert await _count_logs(db_factory) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client: AsyncClient) -> None:
        response = await client.delete("/api/v1/search/history/99999")

        assert response.status_code == 404


class TestClearSearchHistory:
    """DELETE /api/v1/search/history."""

    @pytest.mark.asyncio
    async def test_clears_all_search_history_records(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _ensure_issue(db_factory, 2.0)
        await _seed_search_log(
            db_factory,
            issue_id=issue_id,
            search_type=SearchType.MANUAL,
            confidence="high",
        )
        await _seed_search_log(
            db_factory,
            issue_id=issue_id,
            search_type=SearchType.AUTOMATED,
            confidence="medium",
        )

        response = await client.delete("/api/v1/search/history")

        assert response.status_code == 200
        assert response.json() == {"deleted": 2}
        assert await _count_logs(db_factory) == 0

    @pytest.mark.asyncio
    async def test_requires_authentication(self, unauthed_client: AsyncClient) -> None:
        response = await unauthed_client.delete("/api/v1/search/history")

        assert response.status_code == 401
