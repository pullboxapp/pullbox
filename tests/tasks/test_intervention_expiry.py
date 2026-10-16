"""Tests for the pending match expiry cleanup task — ESM Phase 2, Task 2.7.

Covers:
- Stale pending matches are expired after configured threshold
- Configurable expiry threshold via SystemConfig
- Expiry count is logged
- Recent pending matches are not expired

Run:
    pytest tests/tasks/test_intervention_expiry.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-intervention-expiry")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_pending_matches(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 3,
    age_days: int = 31,
) -> list[int]:
    """Seed a series, issue, and pending matches with configurable age. Returns PM IDs."""
    async with factory() as session:
        series = Series(
            comicvine_id=99900,
            title="Batman",
            sort_title="Batman",
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
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.flush()

        pm_ids: list[int] = []
        created = datetime.now(UTC) - timedelta(days=age_days)
        for i in range(count):
            pm = PendingMatch(
                issue_id=issue.id,
                release_title=f"Batman 001 (2016) Release{i}.cbz",
                download_url=f"https://indexer.example.com/dl/test{i}",
                is_torrent=False,
                file_size=100_000_000,
                confidence="medium",
                match_details={"indexer_name": "NZBgeek"},
                status=PendingMatchStatus.PENDING,
            )
            session.add(pm)
            await session.flush()
            pm_ids.append(pm.id)

        # Backdate created_at
        await session.execute(
            update(PendingMatch).where(PendingMatch.id.in_(pm_ids)).values(created_at=created)
        )
        await session.commit()
        return pm_ids


# ── Tests ──────────────────────────────────────────────────────────────


class TestExpiryTask:
    """Tests for the expire_pending_matches scheduled task."""

    @pytest.mark.asyncio
    async def test_expires_old_entries(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Task expires entries older than the default 10-day threshold."""
        from pullbox.tasks.intervention_task import _run_expiry

        pm_ids = await _seed_pending_matches(db_factory, count=3, age_days=11)

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 3

        # Verify status in DB
        async with db_factory() as session:
            for pm_id in pm_ids:
                pm = await session.get(PendingMatch, pm_id)
                assert pm is not None
                assert pm.status == PendingMatchStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_skips_recent_entries(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Task does not expire entries newer than threshold."""
        from pullbox.tasks.intervention_task import _run_expiry

        pm_ids = await _seed_pending_matches(db_factory, count=2, age_days=5)

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 0

        # Verify still PENDING
        async with db_factory() as session:
            for pm_id in pm_ids:
                pm = await session.get(PendingMatch, pm_id)
                assert pm is not None
                assert pm.status == PendingMatchStatus.PENDING

    @pytest.mark.asyncio
    async def test_respects_config_threshold(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Uses search_intervention_expiry_days from SystemConfig."""
        from pullbox.tasks.intervention_task import _run_expiry

        # Seed entries 10 days old
        pm_ids = await _seed_pending_matches(db_factory, count=2, age_days=10)

        # Set config to 7 days (shorter than default 10)
        async with db_factory() as session:
            session.add(SystemConfig(key="search_intervention_expiry_days", value="7"))
            await session.commit()

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 2

        # Verify expired
        async with db_factory() as session:
            for pm_id in pm_ids:
                pm = await session.get(PendingMatch, pm_id)
                assert pm is not None
                assert pm.status == PendingMatchStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_config_threshold_preserves_newer(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Config threshold of 15 days does not expire 10-day-old entries."""
        from pullbox.tasks.intervention_task import _run_expiry

        pm_ids = await _seed_pending_matches(db_factory, count=2, age_days=10)

        async with db_factory() as session:
            session.add(SystemConfig(key="search_intervention_expiry_days", value="15"))
            await session.commit()

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 0

        # Verify still PENDING
        async with db_factory() as session:
            for pm_id in pm_ids:
                pm = await session.get(PendingMatch, pm_id)
                assert pm is not None
                assert pm.status == PendingMatchStatus.PENDING

    @pytest.mark.asyncio
    async def test_returns_expiry_count(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """_run_expiry returns the number of expired entries."""
        from pullbox.tasks.intervention_task import _run_expiry

        await _seed_pending_matches(db_factory, count=3, age_days=11)

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 3

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_to_expire(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """_run_expiry returns 0 when no entries are stale."""
        from pullbox.tasks.intervention_task import _run_expiry

        async with db_factory() as session:
            count = await _run_expiry(session)
            await session.commit()

        assert count == 0


class TestExpireScheduledWrapper:
    """Tests for the full expire_pending_matches() scheduled task wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_commits(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full wrapper calls _run_expiry and commits results to DB."""
        from pullbox.tasks.intervention_task import expire_pending_matches

        await _seed_pending_matches(db_factory, count=2, age_days=31)

        with patch(
            "pullbox.tasks.intervention_task.get_session_factory",
            return_value=db_factory,
        ):
            await expire_pending_matches()

        # Verify committed to DB — no pending matches remain
        async with db_factory() as session:
            pending = await session.execute(
                select(PendingMatch.id).where(PendingMatch.status == PendingMatchStatus.PENDING)
            )
            assert len(pending.all()) == 0

    @pytest.mark.asyncio
    async def test_wrapper_noop_when_nothing_to_expire(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full wrapper runs cleanly when no stale entries exist."""
        from pullbox.tasks.intervention_task import expire_pending_matches

        with patch(
            "pullbox.tasks.intervention_task.get_session_factory",
            return_value=db_factory,
        ):
            await expire_pending_matches()

    @pytest.mark.asyncio
    async def test_wrapper_rolls_back_on_error(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full wrapper rolls back session on exception."""
        from pullbox.tasks.intervention_task import expire_pending_matches

        with (
            patch(
                "pullbox.tasks.intervention_task.get_session_factory",
                return_value=db_factory,
            ),
            patch(
                "pullbox.tasks.intervention_task._run_expiry",
                side_effect=RuntimeError("DB error"),
            ),
            pytest.raises(RuntimeError, match="DB error"),
        ):
            await expire_pending_matches()
