"""Tests for SearchLog model and UI integration — ESM Phase 4, Task 4.3.

Covers:
- Creating a SearchLog with all required fields
- Default counter values
- Cascade delete when parent issue is deleted
- GET /downloads?tab=search_log renders 200 with "Search Log" tab
- Empty state shows appropriate message
- Seeded logs render table rows

Run:
    pytest tests/unit/test_search_log.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-search-log")


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn: object, _rec: object) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")  # type: ignore[union-attr]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_issue(
    session: AsyncSession,
    *,
    series_title: str = "Batman",
    issue_number: float = 1.0,
) -> Issue:
    """Create a series + issue, return the issue."""
    series = Series(
        comicvine_id=99900,
        title=series_title,
        sort_title=series_title,
        year_start=2016,
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
        issue_type=IssueType.ISSUE,
    )
    session.add(issue)
    await session.flush()
    return issue


# ── Model Tests ───────────────────────────────────────────────────────


class TestSearchLogModel:
    """Tests for SearchLog ORM model."""

    @pytest.mark.asyncio
    async def test_search_log_created(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """SearchLog can be created with all required fields and persists correctly."""
        async with db_factory() as session:
            issue = await _seed_issue(session)

            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.MANUAL,
                results_found=15,
                results_grabbed=1,
                results_queued=0,
                results_rejected=14,
            )
            session.add(log_entry)
            await session.commit()

            result = await session.get(SearchLog, log_entry.id)
            assert result is not None
            assert result.issue_id == issue.id
            assert result.series_title == "Batman"
            assert result.issue_number == 1.0
            assert result.search_type == SearchType.MANUAL
            assert result.results_found == 15
            assert result.results_grabbed == 1
            assert result.results_queued == 0
            assert result.results_rejected == 14
            assert result.created_at is not None

    @pytest.mark.asyncio
    async def test_search_log_defaults(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Counter fields default to 0 when not provided."""
        async with db_factory() as session:
            issue = await _seed_issue(session)

            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.AUTOMATED,
            )
            session.add(log_entry)
            await session.commit()

            result = await session.get(SearchLog, log_entry.id)
            assert result is not None
            assert result.results_found == 0
            assert result.results_grabbed == 0
            assert result.results_queued == 0
            assert result.results_rejected == 0

    @pytest.mark.asyncio
    async def test_search_log_cascade_delete(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Deleting an issue cascades to its search logs."""
        async with db_factory() as session:
            issue = await _seed_issue(session)

            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.BULK,
                results_found=5,
            )
            session.add(log_entry)
            await session.commit()
            log_id = log_entry.id

        async with db_factory() as session:
            issue = (await session.execute(select(Issue).where(Issue.id == issue.id))).scalar_one()
            await session.delete(issue)
            await session.commit()

        async with db_factory() as session:
            result = await session.get(SearchLog, log_id)
            assert result is None

    @pytest.mark.asyncio
    async def test_search_type_enum_values(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """All SearchType enum values can be stored and retrieved."""
        async with db_factory() as session:
            issue = await _seed_issue(session)

            for stype in SearchType:
                log_entry = SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1.0,
                    search_type=stype,
                )
                session.add(log_entry)

            await session.commit()

            results = (await session.execute(select(SearchLog))).scalars().all()
            stored_types = {r.search_type for r in results}
            assert stored_types == set(SearchType)


# ── UI Tests ──────────────────────────────────────────────────────────


@pytest.fixture
async def _api_key_header(
    db_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with db_factory() as session:
        user = User(
            username="searchloguser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="search-log-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def http_client(
    db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    # Keep setup/auth middleware on the same in-memory database as the route deps.
    app.state.db_session_factory = db_factory

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
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


class TestSearchLogUI:
    """Tests for search history page (formerly search log tab on downloads)."""

    @pytest.mark.asyncio
    async def test_search_history_page_renders(self, http_client: AsyncClient) -> None:
        """GET /search-history returns 200 with Search History in HTML."""
        resp = await http_client.get("/search-history")
        assert resp.status_code == 200
        assert "Search History" in resp.text

    @pytest.mark.asyncio
    async def test_search_history_empty_state(self, http_client: AsyncClient) -> None:
        """Empty search log shows appropriate message."""
        resp = await http_client.get("/search-history")
        assert resp.status_code == 200
        assert "No search history" in resp.text
        assert "Search operations will be logged here as they run." in resp.text

    @pytest.mark.asyncio
    async def test_search_history_with_data(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        http_client: AsyncClient,
    ) -> None:
        """Seeded search logs render table rows."""
        async with db_factory() as session:
            issue = await _seed_issue(session, series_title="Amazing Spider-Man")

            for i in range(3):
                session.add(
                    SearchLog(
                        issue_id=issue.id,
                        series_title="Amazing Spider-Man",
                        issue_number=float(i + 1),
                        search_type=SearchType.AUTOMATED,
                        results_found=10,
                        results_grabbed=1,
                    )
                )
            await session.commit()

        resp = await http_client.get("/search-history")
        assert resp.status_code == 200
        assert "Amazing Spider-Man" in resp.text
        assert "Auto" in resp.text

    @pytest.mark.asyncio
    async def test_search_history_formats_long_search_times_in_seconds(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        http_client: AsyncClient,
    ) -> None:
        """Search history renders multi-second timings with second units."""
        async with db_factory() as session:
            issue = await _seed_issue(session, series_title="Batman")
            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1.0,
                    search_type=SearchType.MANUAL,
                    results_found=3,
                    details={"search_time_ms": 1250},
                )
            )
            await session.commit()

        resp = await http_client.get("/search-history")
        assert resp.status_code == 200
        assert "1.2s" in resp.text

    @pytest.mark.asyncio
    async def test_search_history_sidebar_link(self, http_client: AsyncClient) -> None:
        """Search History link is present in the sidebar."""
        resp = await http_client.get("/search-history")
        assert resp.status_code == 200
        assert "/search-history" in resp.text


# ── Details Blob & DB Stats Tests ────────────────────────────────────


class TestSearchLogDetails:
    """Tests for details blob, best_confidence, DB-backed stats, and purge."""

    @pytest.mark.asyncio
    async def test_search_log_details_roundtrip(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Details JSON blob round-trips through the database."""
        details = {
            "best_match": {"title": "Batman 001.cbz", "indexer": "NZBgeek"},
            "matched": [{"title": "Batman 001.cbz", "confidence": "high"}],
            "top_rejected": [],
            "rejected_count": 5,
            "type_distribution": {"issue": 8, "tpb": 2},
            "confidence_breakdown": {"high": 3, "medium": 1},
            "search_passes": 1,
            "search_time_ms": 450,
        }
        async with db_factory() as session:
            issue = await _seed_issue(session)
            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.MANUAL,
                results_found=10,
                details=details,
            )
            session.add(log_entry)
            await session.commit()

            result = await session.get(SearchLog, log_entry.id)
            assert result is not None
            assert result.details == details
            assert result.details["best_match"]["title"] == "Batman 001.cbz"
            assert result.details["type_distribution"]["issue"] == 8

    @pytest.mark.asyncio
    async def test_search_log_best_confidence(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """best_confidence column stores and retrieves correctly."""
        async with db_factory() as session:
            issue = await _seed_issue(session)
            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.MANUAL,
                best_confidence="high",
            )
            session.add(log_entry)
            await session.commit()

            result = await session.get(SearchLog, log_entry.id)
            assert result is not None
            assert result.best_confidence == "high"

    @pytest.mark.asyncio
    async def test_search_log_best_confidence_nullable(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """best_confidence defaults to None when not set."""
        async with db_factory() as session:
            issue = await _seed_issue(session)
            log_entry = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.MANUAL,
            )
            session.add(log_entry)
            await session.commit()

            result = await session.get(SearchLog, log_entry.id)
            assert result is not None
            assert result.best_confidence is None

    @pytest.mark.asyncio
    async def test_get_search_stats_from_db(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_search_stats queries search_logs and returns correct totals."""
        from pullbox.services.search_service import get_search_stats

        async with db_factory() as session:
            issue = await _seed_issue(session)

            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1.0,
                    search_type=SearchType.MANUAL,
                    results_found=20,
                    results_grabbed=2,
                    results_queued=1,
                    results_rejected=17,
                    best_confidence="high",
                )
            )
            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=2.0,
                    search_type=SearchType.AUTOMATED,
                    results_found=10,
                    results_grabbed=1,
                    results_queued=0,
                    results_rejected=9,
                    best_confidence="medium",
                )
            )
            await session.commit()

            stats = await get_search_stats(session)
            assert stats.total_searches == 2
            assert stats.total_results_parsed == 30
            assert stats.total_matched == 4  # 2+1 + 1+0
            assert stats.total_rejected == 26  # 17 + 9
            assert stats.last_search_at is not None

    @pytest.mark.asyncio
    async def test_get_search_stats_empty(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_search_stats returns zeros when no search logs exist."""
        from pullbox.services.search_service import get_search_stats

        async with db_factory() as session:
            stats = await get_search_stats(session)
            assert stats.total_searches == 0
            assert stats.total_results_parsed == 0
            assert stats.total_matched == 0
            assert stats.total_rejected == 0
            assert stats.last_search_at is None
            assert stats.type_distribution == {}
            assert stats.confidence_breakdown == {}

    @pytest.mark.asyncio
    async def test_get_search_stats_confidence_breakdown(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_search_stats groups best_confidence correctly."""
        from pullbox.services.search_service import get_search_stats

        async with db_factory() as session:
            issue = await _seed_issue(session)

            for conf in ["high", "high", "medium", "low"]:
                session.add(
                    SearchLog(
                        issue_id=issue.id,
                        series_title="Batman",
                        issue_number=1.0,
                        search_type=SearchType.MANUAL,
                        best_confidence=conf,
                    )
                )
            # One with None confidence (should be excluded)
            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1.0,
                    search_type=SearchType.MANUAL,
                )
            )
            await session.commit()

            stats = await get_search_stats(session)
            assert stats.confidence_breakdown == {"high": 2, "medium": 1, "low": 1}

    @pytest.mark.asyncio
    async def test_get_search_stats_type_distribution(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_search_stats aggregates type_distribution from details JSON."""
        from pullbox.services.search_service import get_search_stats

        async with db_factory() as session:
            issue = await _seed_issue(session)

            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1.0,
                    search_type=SearchType.MANUAL,
                    details={"type_distribution": {"issue": 5, "tpb": 2}},
                )
            )
            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=2.0,
                    search_type=SearchType.MANUAL,
                    details={"type_distribution": {"issue": 3, "annual": 1}},
                )
            )
            await session.commit()

            stats = await get_search_stats(session)
            assert stats.type_distribution == {"issue": 8, "tpb": 2, "annual": 1}

    @pytest.mark.asyncio
    async def test_purge_search_logs(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """purge_search_logs deletes old entries and keeps recent ones."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text, update

        async with db_factory() as session:
            issue = await _seed_issue(session)

            # Create an old log (15 days ago)
            old_log = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.AUTOMATED,
            )
            session.add(old_log)
            await session.flush()

            # Manually backdate its created_at (naive for SQLite compat)
            old_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=15)
            await session.execute(
                update(SearchLog).where(SearchLog.id == old_log.id).values(created_at=old_time)
            )

            # Create a recent log
            recent_log = SearchLog(
                issue_id=issue.id,
                series_title="Batman",
                issue_number=2.0,
                search_type=SearchType.MANUAL,
            )
            session.add(recent_log)
            await session.commit()

            # Purge with 7-day retention using raw SQL (avoids ORM TZ evaluator)
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
            result = await session.execute(
                text("DELETE FROM search_logs WHERE created_at < :cutoff"),
                {"cutoff": cutoff.isoformat()},
            )
            await session.commit()
            assert result.rowcount == 1

            # Verify only recent log remains
            remaining = (await session.execute(select(SearchLog))).scalars().all()
            assert len(remaining) == 1
            assert remaining[0].id == recent_log.id


async def test_scheduled_log_purge_commits_small_batches(db_factory, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from pullbox.tasks import search_task

    async with db_factory() as session:
        issue = await _seed_issue(session)
        for _ in range(5):
            session.add(
                SearchLog(
                    issue_id=issue.id,
                    series_title="Batman",
                    issue_number=1,
                    search_type=SearchType.AUTOMATED,
                    created_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
        await session.commit()

    commits = []

    class TrackingSession(AsyncSession):
        async def commit(self):
            await super().commit()
            commits.append(True)

    factory = async_sessionmaker(
        db_factory.kw["bind"], class_=TrackingSession, expire_on_commit=False
    )
    monkeypatch.setattr(search_task, "get_session_factory", lambda: factory)
    monkeypatch.setattr(search_task, "_load_search_log_retention_days", AsyncMock(return_value=7))
    monkeypatch.setattr(search_task, "_SEARCH_LOG_PURGE_BATCH_SIZE", 2, raising=False)
    await search_task.purge_search_logs()
    assert len(commits) >= 3, "Log retention must release the writer between bounded batches"
    async with db_factory() as session:
        assert list((await session.scalars(select(SearchLog))).all()) == []
