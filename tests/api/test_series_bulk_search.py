"""Tests for the async bulk search endpoint (POST /api/v1/series/{id}/search).

Verifies that:
- The endpoint returns immediately with task status
- Only WANTED issues are counted
- No-wanted case skips task launch
- Nonexistent series returns 404

Run:
    pytest tests/api/test_series_bulk_search.py -v
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
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-bulk-search")


# ── Fixtures ──────────────────────────────────────────────────────────


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
    seeded_user_password_hash: str,
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="bulkuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="bulk-test"))
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


def _seed_series(
    _db_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Helper — returns the series_id after seeding."""
    raise NotImplementedError  # see the per-test inline seeding


async def _create_series_with_issues(
    factory: async_sessionmaker[AsyncSession],
    *,
    wanted: int = 0,
    skipped: int = 0,
    owned: int = 0,
) -> int:
    """Seed a series with the given issue counts, return series_id."""
    async with factory() as session:
        series = Series(
            comicvine_id=99999,
            title="Bulk Test Series",
            sort_title="Bulk Test Series",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=wanted + skipped + owned,
        )
        session.add(series)
        await session.flush()
        sid = series.id

        num = 1
        for _ in range(wanted):
            session.add(
                Issue(
                    series_id=sid,
                    comicvine_id=10000 + num,
                    issue_number=float(num),
                    title=f"Issue #{num}",
                    status=IssueStatus.WANTED,
                )
            )
            num += 1
        for _ in range(skipped):
            session.add(
                Issue(
                    series_id=sid,
                    comicvine_id=10000 + num,
                    issue_number=float(num),
                    title=f"Issue #{num}",
                    status=IssueStatus.SKIPPED,
                )
            )
            num += 1
        for _ in range(owned):
            session.add(
                Issue(
                    series_id=sid,
                    comicvine_id=10000 + num,
                    issue_number=float(num),
                    title=f"Issue #{num}",
                    status=IssueStatus.OWNED,
                )
            )
            num += 1

        await session.commit()
    return sid


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_all_wanted_returns_immediately(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST returns task status with issues_to_search count and status='started'."""
    sid = await _create_series_with_issues(_db_factory, wanted=5)

    with patch(
        "pullbox.api.v1.series.search_series_issues",
        new_callable=AsyncMock,
        return_value={"wanted": 5, "sent": 0},
    ) as mock_search:
        resp = await client.post(f"/api/v1/series/{sid}/search")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    assert data["series_id"] == sid
    assert data["issues_to_search"] == 5
    assert "task_id" in data
    assert data["message"] == "Search started for 5 issues"

    async with _db_factory() as session:
        result = await session.execute(select(SearchLog).order_by(SearchLog.id))
        logs = list(result.scalars().all())

    assert len(logs) == 5
    assert all(log.search_type == SearchType.BULK for log in logs)
    assert all((log.details or {}).get("run_state") == "running" for log in logs)

    mock_search.assert_called_once()
    assert mock_search.await_args.args == (sid,)
    assert mock_search.await_args.kwargs["pending_log_ids_by_issue"] == {
        log.issue_id: log.id for log in logs
    }


@pytest.mark.asyncio
async def test_search_all_wanted_searches_only_wanted(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed WANTED + SKIPPED + OWNED issues; issues_to_search counts only WANTED."""
    sid = await _create_series_with_issues(_db_factory, wanted=3, skipped=2, owned=4)

    with patch(
        "pullbox.api.v1.series.search_series_issues",
        new_callable=AsyncMock,
        return_value={"wanted": 3, "sent": 0},
    ):
        resp = await client.post(f"/api/v1/series/{sid}/search")

    assert resp.status_code == 200
    data = resp.json()
    assert data["issues_to_search"] == 3


@pytest.mark.asyncio
async def test_search_no_wanted_issues(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Series with no WANTED issues returns issues_to_search=0 without launching a task."""
    sid = await _create_series_with_issues(_db_factory, wanted=0, owned=5)

    with patch(
        "pullbox.api.v1.series.search_series_issues",
        new_callable=AsyncMock,
    ) as mock_search:
        resp = await client.post(f"/api/v1/series/{sid}/search")

    assert resp.status_code == 200
    data = resp.json()
    assert data["issues_to_search"] == 0
    assert data["status"] == "no_wanted"
    assert data["message"] == "No wanted issues to search"
    # Should NOT have launched a background task
    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_search_all_wanted_htmx_launch_sets_toast_header(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTMX launch requests receive a server-triggered toast for pull-list actions."""
    sid = await _create_series_with_issues(_db_factory, wanted=2)

    with patch(
        "pullbox.api.v1.series.search_series_issues",
        new_callable=AsyncMock,
        return_value={"wanted": 2, "sent": 0},
    ):
        resp = await client.post(
            f"/api/v1/series/{sid}/search",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert '"message": "Search started for 2 issues"' in resp.headers["HX-Trigger"]


@pytest.mark.asyncio
async def test_search_nonexistent_series_returns_404(client: AsyncClient) -> None:
    """Missing series returns 404."""
    resp = await client.post("/api/v1/series/99999/search")
    assert resp.status_code == 404
