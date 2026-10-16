"""Unit tests for monitoring toggle workflow.

Tests the toggle_monitoring service method and its effects on issue statuses.
Covers: ON→OFF transitions, OFF→ON transitions, manual_skip preservation,
download cancellation, OWNED untouched, edge cases.

Run:
    pytest tests/unit/test_monitoring_toggle.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.events import EventBus
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series, SeriesStatus
from pullbox.services.series_service import SeriesService
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-monitoring")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory DB with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh event bus for each test."""
    return EventBus()


@pytest.fixture
def series_svc(event_bus: EventBus) -> SeriesService:
    """SeriesService with mocked metadata."""
    mock_metadata = AsyncMock()
    return SeriesService(metadata_service=mock_metadata, event_bus=event_bus)


async def _seed_series(
    factory: async_sessionmaker[AsyncSession],
    monitored: bool = False,
    issues: list[tuple[float, IssueStatus, bool]] | None = None,
) -> int:
    """Create a series with issues. Returns series ID.

    issues: list of (issue_number, status, manual_skip) tuples.
    """
    async with factory() as session:
        series = Series(
            comicvine_id=99999,
            title="Test Series",
            sort_title="Test Series",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            monitored=monitored,
        )
        session.add(series)
        await session.flush()
        series_id = series.id

        if issues:
            for num, status, manual_skip in issues:
                issue = Issue(
                    series_id=series_id,
                    comicvine_id=100000 + int(num),
                    issue_number=num,
                    title=f"Issue #{int(num)}",
                    status=status,
                    manual_skip=manual_skip,
                )
                session.add(issue)

        await session.commit()
    return series_id


async def _get_issue_statuses(
    factory: async_sessionmaker[AsyncSession],
    series_id: int,
) -> dict[float, tuple[IssueStatus, bool]]:
    """Return {issue_number: (status, manual_skip)} for all issues in a series."""
    async with factory() as session:
        result = await session.execute(
            select(Issue).where(Issue.series_id == series_id).order_by(Issue.issue_number)
        )
        return {i.issue_number: (i.status, i.manual_skip) for i in result.scalars().all()}


# ── Toggle ON tests (False → True) ───────────────────────────────────────


@pytest.mark.asyncio
class TestToggleMonitoringOn:
    """Enabling monitoring: SKIPPED → WANTED, respects manual_skip."""

    async def test_skipped_become_wanted(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """All SKIPPED issues move to WANTED when monitoring is enabled."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.SKIPPED, False),
                (2.0, IssueStatus.SKIPPED, False),
                (3.0, IssueStatus.SKIPPED, False),
            ],
        )

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()
            assert result.monitored is True

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.WANTED, False)
        assert statuses[2.0] == (IssueStatus.WANTED, False)
        assert statuses[3.0] == (IssueStatus.WANTED, False)

    async def test_manual_skip_preserved(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Issues with manual_skip=True stay SKIPPED when monitoring is enabled."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.SKIPPED, False),
                (2.0, IssueStatus.SKIPPED, True),  # manually skipped
                (3.0, IssueStatus.SKIPPED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.WANTED, False)
        assert statuses[2.0] == (IssueStatus.SKIPPED, True)  # stays skipped
        assert statuses[3.0] == (IssueStatus.WANTED, False)

    async def test_owned_untouched(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """OWNED issues are never changed when toggling monitoring on."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.OWNED, False),
                (2.0, IssueStatus.SKIPPED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.OWNED, False)
        assert statuses[2.0] == (IssueStatus.WANTED, False)

    async def test_already_wanted_stay_wanted(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Issues already WANTED are not affected by toggling on."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.WANTED, False),
                (2.0, IssueStatus.SKIPPED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.WANTED, False)
        assert statuses[2.0] == (IssueStatus.WANTED, False)

    async def test_no_issues(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Toggling on a series with no issues succeeds without error."""
        series_id = await _seed_series(db_factory, monitored=False, issues=[])

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()
            assert result.monitored is True

    async def test_noop_when_already_on(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Toggling on when already monitored is a no-op."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.SKIPPED, False),
            ],
        )

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()
            assert result.monitored is True

        # Issue should NOT have been changed (no-op)
        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.SKIPPED, False)

    async def test_mixed_statuses(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Mixed statuses: only SKIPPED (non-manual_skip) become WANTED."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.OWNED, False),
                (2.0, IssueStatus.SKIPPED, False),
                (3.0, IssueStatus.SKIPPED, True),
                (4.0, IssueStatus.WANTED, False),
                (5.0, IssueStatus.DOWNLOADING, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0][0] == IssueStatus.OWNED  # untouched
        assert statuses[2.0][0] == IssueStatus.WANTED  # flipped
        assert statuses[3.0][0] == IssueStatus.SKIPPED  # manual_skip preserved
        assert statuses[4.0][0] == IssueStatus.WANTED  # already wanted
        assert statuses[5.0][0] == IssueStatus.DOWNLOADING  # untouched


# ── Toggle OFF tests (True → False) ──────────────────────────────────────


@pytest.mark.asyncio
class TestToggleMonitoringOff:
    """Disabling monitoring: WANTED/DOWNLOADING → SKIPPED."""

    async def test_wanted_become_skipped(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """All WANTED issues move to SKIPPED when monitoring is disabled."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.WANTED, False),
                (2.0, IssueStatus.WANTED, False),
                (3.0, IssueStatus.WANTED, False),
            ],
        )

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()
            assert result.monitored is False

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.SKIPPED, False)
        assert statuses[2.0] == (IssueStatus.SKIPPED, False)
        assert statuses[3.0] == (IssueStatus.SKIPPED, False)

    async def test_downloading_become_skipped(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """DOWNLOADING issues move to SKIPPED when monitoring is disabled."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.DOWNLOADING, False),
                (2.0, IssueStatus.WANTED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.SKIPPED, False)
        assert statuses[2.0] == (IssueStatus.SKIPPED, False)

    async def test_owned_untouched(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """OWNED issues are never changed when toggling monitoring off."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.OWNED, False),
                (2.0, IssueStatus.WANTED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.OWNED, False)
        assert statuses[2.0] == (IssueStatus.SKIPPED, False)

    async def test_already_skipped_stay_skipped(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Already SKIPPED issues remain SKIPPED."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.SKIPPED, False),
                (2.0, IssueStatus.SKIPPED, True),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.SKIPPED, False)
        assert statuses[2.0] == (IssueStatus.SKIPPED, True)  # manual_skip preserved

    async def test_noop_when_already_off(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Toggling off when already unmonitored is a no-op."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.WANTED, False),
            ],
        )

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()
            assert result.monitored is False

        # Issue should NOT have been changed (no-op)
        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0] == (IssueStatus.WANTED, False)

    async def test_no_issues(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Toggling off a series with no issues succeeds without error."""
        series_id = await _seed_series(db_factory, monitored=True, issues=[])

        async with db_factory() as session:
            result = await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()
            assert result.monitored is False

    async def test_all_owned_no_changes(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Series where all issues are OWNED — toggle off changes nothing."""
        series_id = await _seed_series(
            db_factory,
            monitored=True,
            issues=[
                (1.0, IssueStatus.OWNED, False),
                (2.0, IssueStatus.OWNED, False),
            ],
        )

        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0][0] == IssueStatus.OWNED
        assert statuses[2.0][0] == IssueStatus.OWNED


# ── Round-trip toggle tests ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestToggleRoundTrip:
    """Toggling on then off then on preserves manual_skip."""

    async def test_on_off_on_preserves_manual_skip(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """manual_skip survives a full on→off→on cycle."""
        series_id = await _seed_series(
            db_factory,
            monitored=False,
            issues=[
                (1.0, IssueStatus.SKIPPED, False),
                (2.0, IssueStatus.SKIPPED, True),  # manually skipped
                (3.0, IssueStatus.OWNED, False),
            ],
        )

        # Toggle ON
        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0][0] == IssueStatus.WANTED
        assert statuses[2.0][0] == IssueStatus.SKIPPED  # manual_skip preserved
        assert statuses[3.0][0] == IssueStatus.OWNED

        # Toggle OFF
        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=False)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0][0] == IssueStatus.SKIPPED
        assert statuses[2.0][0] == IssueStatus.SKIPPED
        assert statuses[3.0][0] == IssueStatus.OWNED

        # Toggle ON again
        async with db_factory() as session:
            await series_svc.toggle_monitoring(session, series_id, enabled=True)
            await session.commit()

        statuses = await _get_issue_statuses(db_factory, series_id)
        assert statuses[1.0][0] == IssueStatus.WANTED
        assert statuses[2.0][0] == IssueStatus.SKIPPED  # STILL manually skipped
        assert statuses[2.0][1] is True  # manual_skip flag intact
        assert statuses[3.0][0] == IssueStatus.OWNED

    async def test_not_found_raises(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_svc: SeriesService,
    ) -> None:
        """Toggling a non-existent series raises NotFoundError."""
        from pullbox.core.exceptions import NotFoundError

        async with db_factory() as session:
            with pytest.raises(NotFoundError):
                await series_svc.toggle_monitoring(session, 99999, enabled=True)
