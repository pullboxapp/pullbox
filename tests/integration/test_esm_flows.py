"""End-to-end integration tests for ESM search, routing, and purge flows.

Covers:
- search_series_issues: auto-grab (high confidence) and queue (medium) routing
- search_series_issues: pass-2 generic fallback for non-standard types
- search_series_issues: no-indexer early exit
- search_series_issues: no-wanted-issues early exit
- search_series_issues: series-not-found early exit
- search_series_issues: send failure / queue failure rollback paths
- search_wanted: end-to-end with auto-grab and queue routing
- search_wanted: no-results early exit
- search_wanted: search failure rollback
- purge_search_logs: respects config retention, purges old entries
- SearchService.search_for_issue: issue/series resolution and query building
- SearchService.search_wanted: query fan-out across wanted issues
- SearchService._search_indexers: fan-out, category filtering, health tracking
- SearchService._search_single_indexer: success resets health, error applies backoff
- Helper functions: _is_comic_category, _extract_subtitle, calculate_backoff

Run:
    pytest tests/integration/test_esm_flows.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.publisher import Publisher
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import ReleaseResult, SearchQuery
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-esm-flows")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── Seed Helpers ──────────────────────────────────────────────────────


async def _seed_series_and_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    issue_number: float = 1.0,
    issue_type: IssueType = IssueType.ISSUE,
    year_start: int = 2016,
    alternate_names: list[str] | None = None,
) -> tuple[int, int]:
    """Seed a series + wanted issue. Returns (series_id, issue_id)."""
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
            alternate_names=alternate_names,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=issue_number,
            title=f"Issue #{int(issue_number)}",
            status=IssueStatus.WANTED,
            issue_type=issue_type,
        )
        session.add(issue)
        await session.commit()
        return series.id, issue.id


async def _seed_multiple_wanted(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    count: int = 3,
) -> tuple[int, list[int]]:
    """Seed a series with N wanted issues. Returns (series_id, [issue_ids])."""
    async with factory() as session:
        series = Series(
            comicvine_id=99900,
            title=series_title,
            sort_title=series_title,
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=count,
        )
        session.add(series)
        await session.flush()
        sid = series.id

        issue_ids = []
        for i in range(1, count + 1):
            issue = Issue(
                series_id=sid,
                comicvine_id=50000 + i,
                issue_number=float(i),
                title=f"Issue #{i}",
                status=IssueStatus.WANTED,
                issue_type=IssueType.ISSUE,
            )
            session.add(issue)
            await session.flush()
            issue_ids.append(issue.id)

        await session.commit()
        return sid, issue_ids


def _make_release(
    title: str,
    *,
    indexer_name: str = "NZBgeek",
    size_mb: float = 100.0,
    is_torrent: bool = False,
    category: str = "7030",
) -> ReleaseResult:
    """Create a ReleaseResult for testing."""
    return ReleaseResult(
        title=title,
        indexer_name=indexer_name,
        download_url=f"https://test.com/nzb/{title.replace(' ', '_')}",
        size_bytes=int(size_mb * 1024 * 1024),
        age_days=5,
        seeders=10 if is_torrent else None,
        leechers=2 if is_torrent else None,
        grabs=10,
        is_torrent=is_torrent,
        category=category,
        published_at=datetime.now(UTC) - timedelta(days=5),
    )


def _mock_registry_with_results(results: list[ReleaseResult]) -> MagicMock:
    """Create a mock ProviderRegistry with a single indexer returning *results*."""
    indexer = AsyncMock()
    indexer.name = "MockIndexer"
    indexer.search = AsyncMock(return_value=results)
    registry = MagicMock()
    registry.get_indexer_items.return_value = [(1, indexer)]
    return registry


# ── search_series_issues Tests ────────────────────────────────────────


class TestSearchSeriesIssues:
    """Integration tests for the search_series_issues task function."""

    @pytest.mark.asyncio
    async def test_auto_grab_high_confidence(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """High-confidence match on a standard issue triggers auto-grab."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (2016) (Digital) (Shan-Empire).cbz")]
        mock_registry = _mock_registry_with_results(results)
        mock_download_svc = AsyncMock()
        mock_download_svc.send_to_client = AsyncMock()

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService", return_value=mock_download_svc),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=results)

            # evaluate_results returns best match
            mock_svc.evaluate_results = MagicMock(return_value=results[0])

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        assert stats["wanted"] == 1
        assert stats["sent"] == 1
        assert stats["queued"] == 0
        mock_download_svc.send_to_client.assert_called_once()

        # Verify SearchLog was written
        async with db_factory() as session:
            logs = (await session.execute(select(SearchLog))).scalars().all()
            assert len(logs) == 1
            assert logs[0].results_grabbed == 1
            assert logs[0].search_type == SearchType.BULK

    @pytest.mark.asyncio
    async def test_queue_medium_confidence(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Medium-confidence match on a standard issue routes to intervention queue."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (Digital).cbz")]
        mock_registry = _mock_registry_with_results(results)
        mock_download_svc = AsyncMock()

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService", return_value=mock_download_svc),
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=results)
            mock_svc.evaluate_results = MagicMock(return_value=results[0])

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc
            mock_isvc.has_pending_for_issue = AsyncMock(return_value=False)
            mock_isvc.create_pending_match = AsyncMock()

            # Override should_auto_grab to return False (medium confidence)
            with patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=False,
            ):
                from pullbox.tasks.search_task import search_series_issues

                stats = await search_series_issues(series_id)

        assert stats["wanted"] == 1
        assert stats["sent"] == 0
        assert stats["queued"] == 1
        mock_isvc.create_pending_match.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_indexers_early_exit(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Returns zeros when no indexers are configured."""
        series_id, _ = await _seed_series_and_issue(db_factory)

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.search_task.build_registry", return_value=None),
        ):
            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        assert stats == {"wanted": 0, "sent": 0, "queued": 0}

    @pytest.mark.asyncio
    async def test_series_not_found(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Returns zeros when series doesn't exist."""
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
        ):
            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(99999)

        assert stats == {"wanted": 0, "sent": 0, "queued": 0}

    @pytest.mark.asyncio
    async def test_no_wanted_issues(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Returns zeros when series has no wanted issues."""
        async with db_factory() as session:
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
            sid = series.id
            # Add issue as DOWNLOADED (not WANTED)
            issue = Issue(
                series_id=sid,
                comicvine_id=50001,
                issue_number=1.0,
                title="Issue #1",
                status=IssueStatus.OWNED,
                issue_type=IssueType.ISSUE,
            )
            session.add(issue)
            await session.commit()

        mock_registry = _mock_registry_with_results([])
        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
        ):
            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(sid)

        assert stats == {"wanted": 0, "sent": 0, "queued": 0}

    @pytest.mark.asyncio
    async def test_send_failure_rolls_back(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Download send failure triggers rollback and continues."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (2016) (Digital).cbz")]
        mock_registry = _mock_registry_with_results(results)
        mock_download_svc = AsyncMock()
        mock_download_svc.send_to_client = AsyncMock(side_effect=RuntimeError("Send failed"))

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService", return_value=mock_download_svc),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=results)
            mock_svc.evaluate_results = MagicMock(return_value=results[0])

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        # Send failed → 0 sent, but should not crash
        assert stats["sent"] == 0
        assert stats["wanted"] == 1

    @pytest.mark.asyncio
    async def test_queue_failure_rolls_back(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Queue creation failure triggers rollback and continues."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (Digital).cbz")]
        mock_registry = _mock_registry_with_results(results)

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService"),
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=False,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=results)
            mock_svc.evaluate_results = MagicMock(return_value=results[0])

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc
            mock_isvc.has_pending_for_issue = AsyncMock(return_value=False)
            mock_isvc.create_pending_match = AsyncMock(side_effect=RuntimeError("Queue failed"))

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        assert stats["queued"] == 0
        assert stats["wanted"] == 1

    @pytest.mark.asyncio
    async def test_pending_exists_skips_queue(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When a pending match already exists, the issue is skipped."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (Digital).cbz")]
        mock_registry = _mock_registry_with_results(results)

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService"),
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=False,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=results)
            mock_svc.evaluate_results = MagicMock(return_value=results[0])

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc
            mock_isvc.has_pending_for_issue = AsyncMock(return_value=True)

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        assert stats["queued"] == 0
        mock_isvc.create_pending_match.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_results_logs_search(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When search returns no results, a SearchLog is still written."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory)

        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService"),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(return_value=[])
            mock_svc.evaluate_results = MagicMock(return_value=None)

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        assert stats["sent"] == 0
        assert stats["queued"] == 0

        async with db_factory() as session:
            logs = (await session.execute(select(SearchLog))).scalars().all()
            assert len(logs) == 1
            assert logs[0].results_found == 0

    @pytest.mark.asyncio
    async def test_pass2_generic_for_tpb(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Non-standard type (TPB) triggers pass-2 generic search when pass 1 misses."""
        series_id, _issue_id = await _seed_series_and_issue(db_factory, issue_type=IssueType.TPB)

        pass1_results: list[ReleaseResult] = []
        pass2_results = [_make_release("Batman Vol 01 (2016).cbz")]
        mock_registry = _mock_registry_with_results([])

        async def _mock_search(
            session: object, issue_id: int, **kwargs: object
        ) -> list[ReleaseResult]:
            if kwargs.get("force_generic"):
                return pass2_results
            return pass1_results

        # Mock validator that always returns a valid result
        mock_validator = MagicMock()
        mock_validation = MagicMock()
        mock_validation.confidence = MagicMock()
        mock_validation.confidence.value = "medium"
        mock_validator.validate_results = MagicMock(return_value=[mock_validation])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService"),
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch("pullbox.tasks.search_task.ReleaseValidator", return_value=mock_validator),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_for_issue = AsyncMock(side_effect=_mock_search)

            # Pass 1 returns [] so evaluate_results is NOT called for pass 1.
            # Only pass 2 calls evaluate_results — return the best match.
            mock_svc.evaluate_results = MagicMock(return_value=pass2_results[0])

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc
            mock_isvc.has_pending_for_issue = AsyncMock(return_value=False)
            mock_isvc.create_pending_match = AsyncMock()

            from pullbox.tasks.search_task import search_series_issues

            stats = await search_series_issues(series_id)

        # Pass 2 results always queue, never auto-grab
        assert stats["queued"] == 1
        assert stats["sent"] == 0


# ── search_wanted Task Tests ─────────────────────────────────────────


class TestSearchWanted:
    """Integration tests for the search_wanted scheduled task."""

    @pytest.mark.asyncio
    async def test_search_wanted_auto_grab(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted processes issues and auto-grabs high-confidence matches."""
        _series_id, issue_ids = await _seed_multiple_wanted(db_factory, count=2)

        r1 = _make_release("Batman 001 (2016) (Digital).cbz")
        r2 = _make_release("Batman 002 (2016) (Digital).cbz")
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService") as mock_dl_cls,
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=True,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_wanted = AsyncMock(
                return_value={issue_ids[0]: [r1], issue_ids[1]: [r2]}
            )
            mock_svc.evaluate_results = MagicMock(side_effect=[r1, r2])

            mock_dl_svc = AsyncMock()
            mock_dl_cls.return_value = mock_dl_svc

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc

            from pullbox.tasks.search_task import search_wanted

            await search_wanted()

        assert mock_dl_svc.send_to_client.call_count == 2

        # Verify SearchLogs were written
        async with db_factory() as session:
            logs = (await session.execute(select(SearchLog))).scalars().all()
            assert len(logs) == 2
            for log in logs:
                assert log.search_type == SearchType.AUTOMATED
                assert log.results_grabbed == 1

    @pytest.mark.asyncio
    async def test_search_wanted_queue_routing(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted routes medium-confidence matches to the intervention queue."""
        _series_id, issue_ids = await _seed_multiple_wanted(db_factory, count=1)

        r1 = _make_release("Batman 001 (Digital).cbz")
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService") as mock_dl_cls,
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=False,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_wanted = AsyncMock(return_value={issue_ids[0]: [r1]})
            mock_svc.evaluate_results = MagicMock(return_value=r1)

            mock_dl_svc = AsyncMock()
            mock_dl_cls.return_value = mock_dl_svc

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc
            mock_isvc.has_pending_for_issue = AsyncMock(return_value=False)

            from pullbox.tasks.search_task import search_wanted

            await search_wanted()

        mock_isvc.create_pending_match.assert_called_once()
        mock_dl_svc.send_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_wanted_no_indexers(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted exits early when no indexers configured."""
        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.search_task.build_registry", return_value=None),
        ):
            from pullbox.tasks.search_task import search_wanted

            await search_wanted()  # Should not raise

    @pytest.mark.asyncio
    async def test_search_wanted_no_results(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted exits cleanly when search returns empty map."""
        await _seed_multiple_wanted(db_factory, count=1)
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService"),
            patch("pullbox.tasks.search_task.InterventionService"),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_wanted = AsyncMock(return_value={})

            from pullbox.tasks.search_task import search_wanted

            await search_wanted()  # Should not raise

    @pytest.mark.asyncio
    async def test_search_wanted_issue_failure_continues(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted logs failure for one issue but continues processing."""
        _series_id, issue_ids = await _seed_multiple_wanted(db_factory, count=2)

        r1 = _make_release("Batman 001 (2016) (Digital).cbz")
        r2 = _make_release("Batman 002 (2016) (Digital).cbz")
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService") as mock_dl_cls,
            patch("pullbox.tasks.search_task.InterventionService"),
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=True,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_wanted = AsyncMock(
                return_value={issue_ids[0]: [r1], issue_ids[1]: [r2]}
            )
            # First call: raises, second call: succeeds
            mock_svc.evaluate_results = MagicMock(side_effect=[RuntimeError("Eval failed"), r2])

            mock_dl_svc = AsyncMock()
            mock_dl_cls.return_value = mock_dl_svc

            from pullbox.tasks.search_task import search_wanted

            await search_wanted()

        # Only the second issue should have been grabbed
        assert mock_dl_svc.send_to_client.call_count == 1

    @pytest.mark.asyncio
    async def test_search_wanted_commits_between_search_and_issue_routing(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted should not hold one write transaction for the whole run."""
        _series_id, issue_ids = await _seed_multiple_wanted(db_factory, count=2)

        class _TrackingFactory:
            def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
                self._factory = factory
                self.commit_calls = 0

            def __call__(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                real_cm = self._factory(*args, **kwargs)
                tracker = self

                class _Wrapper:
                    async def __aenter__(self) -> AsyncSession:
                        session = await real_cm.__aenter__()
                        original_commit = session.commit

                        async def tracked_commit() -> None:
                            tracker.commit_calls += 1
                            await original_commit()

                        session.commit = tracked_commit  # type: ignore[method-assign]
                        return session

                    async def __aexit__(self, exc_type, exc, tb) -> bool | None:  # type: ignore[no-untyped-def]
                        return await real_cm.__aexit__(exc_type, exc, tb)

                return _Wrapper()

        tracking_factory = _TrackingFactory(db_factory)
        r1 = _make_release("Batman 001 (2016) (Digital).cbz")
        r2 = _make_release("Batman 002 (2016) (Digital).cbz")
        mock_registry = _mock_registry_with_results([])

        with (
            patch("pullbox.tasks.search_task.get_session_factory", return_value=tracking_factory),
            patch(
                "pullbox.tasks.search_task.build_registry",
                return_value=(mock_registry, {}),
            ),
            patch("pullbox.tasks.search_task.SearchService") as mock_ss_cls,
            patch("pullbox.tasks.search_task.DownloadService") as mock_dl_cls,
            patch("pullbox.tasks.search_task.InterventionService") as mock_isvc_cls,
            patch(
                "pullbox.services.search_acquisition_router.should_auto_grab",
                return_value=True,
            ),
        ):
            mock_svc = AsyncMock()
            mock_ss_cls.return_value = mock_svc
            mock_svc.search_wanted = AsyncMock(
                return_value={issue_ids[0]: [r1], issue_ids[1]: [r2]}
            )
            mock_svc.evaluate_results = MagicMock(side_effect=[r1, r2])

            mock_dl_svc = AsyncMock()
            mock_dl_cls.return_value = mock_dl_svc

            mock_isvc = AsyncMock()
            mock_isvc_cls.return_value = mock_isvc

            from pullbox.tasks.search_task import search_wanted

            await search_wanted()

        assert tracking_factory.commit_calls >= len(issue_ids) + 1


# ── purge_search_logs Task Tests ─────────────────────────────────────


class TestPurgeSearchLogs:
    """Integration tests for the purge_search_logs scheduled task."""

    @pytest.mark.asyncio
    async def test_purge_removes_old_entries(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """purge_search_logs deletes entries older than retention period."""
        _, issue_id = await _seed_series_and_issue(db_factory)

        async with db_factory() as session:
            # Old log (15 days)
            old_log = SearchLog(
                issue_id=issue_id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.AUTOMATED,
            )
            session.add(old_log)
            await session.flush()

            old_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=15)
            await session.execute(
                update(SearchLog).where(SearchLog.id == old_log.id).values(created_at=old_time)
            )

            # Recent log
            session.add(
                SearchLog(
                    issue_id=issue_id,
                    series_title="Batman",
                    issue_number=2.0,
                    search_type=SearchType.MANUAL,
                )
            )
            await session.commit()

        with patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory):
            from pullbox.tasks.search_task import purge_search_logs

            await purge_search_logs()

        async with db_factory() as session:
            remaining = (await session.execute(select(SearchLog))).scalars().all()
            assert len(remaining) == 1
            assert remaining[0].series_title == "Batman"
            assert remaining[0].issue_number == 2.0

    @pytest.mark.asyncio
    async def test_purge_respects_config_retention(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """purge_search_logs reads retention_days from SystemConfig."""
        _, issue_id = await _seed_series_and_issue(db_factory)

        async with db_factory() as session:
            # Set retention to 30 days
            session.add(SystemConfig(key="search_log_retention_days", value="30"))

            # Create log 15 days old — should NOT be purged with 30-day retention
            old_log = SearchLog(
                issue_id=issue_id,
                series_title="Batman",
                issue_number=1.0,
                search_type=SearchType.AUTOMATED,
            )
            session.add(old_log)
            await session.flush()

            old_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=15)
            await session.execute(
                update(SearchLog).where(SearchLog.id == old_log.id).values(created_at=old_time)
            )
            await session.commit()

        with patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory):
            from pullbox.tasks.search_task import purge_search_logs

            await purge_search_logs()

        async with db_factory() as session:
            remaining = (await session.execute(select(SearchLog))).scalars().all()
            assert len(remaining) == 1  # 15-day-old log preserved with 30-day retention

    @pytest.mark.asyncio
    async def test_purge_nothing_to_delete(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """purge_search_logs runs cleanly when no old entries exist."""
        with patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory):
            from pullbox.tasks.search_task import purge_search_logs

            await purge_search_logs()  # Should not raise


# ── SearchService.search_for_issue Tests ──────────────────────────────


class TestSearchServiceSearchForIssue:
    """Integration tests for SearchService.search_for_issue method."""

    @pytest.mark.asyncio
    async def test_search_for_issue_basic(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_for_issue builds queries from DB issue and searches indexers."""
        _series_id, issue_id = await _seed_series_and_issue(db_factory)

        results = [_make_release("Batman 001 (2016).cbz")]
        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(return_value=results)
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        svc = None
        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            found = await svc.search_for_issue(session, issue_id)

        assert len(found) == 1
        assert found[0].title == "Batman 001 (2016).cbz"
        indexer.search.assert_called()

    @pytest.mark.asyncio
    async def test_search_for_issue_not_found(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_for_issue raises NotFoundError for missing issue."""
        registry = MagicMock()

        async with db_factory() as session:
            from pullbox.core.exceptions import NotFoundError
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            with pytest.raises(NotFoundError):
                await svc.search_for_issue(session, 99999)

    @pytest.mark.asyncio
    async def test_search_for_issue_force_generic(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """force_generic=True builds plain queries without type keywords."""
        _series_id, issue_id = await _seed_series_and_issue(db_factory, issue_type=IssueType.TPB)

        captured_queries: list[SearchQuery] = []

        async def _capture_search(query: SearchQuery) -> list[ReleaseResult]:
            captured_queries.append(query)
            return []

        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(side_effect=_capture_search)
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            await svc.search_for_issue(session, issue_id, force_generic=True)

        # With force_generic, should NOT include "TPB" or "Vol" keywords
        for q in captured_queries:
            assert "TPB" not in q.series_title
            assert "Vol" not in q.series_title

    @pytest.mark.asyncio
    async def test_search_for_issue_dedup(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Duplicate results (same URL) are deduplicated."""
        _series_id, issue_id = await _seed_series_and_issue(db_factory)

        r = _make_release("Batman 001 (2016).cbz")
        # Return same result from multiple queries
        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(return_value=[r])
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            found = await svc.search_for_issue(session, issue_id)

        # Should be deduplicated
        assert len(found) == 1

    @pytest.mark.asyncio
    async def test_search_for_issue_collection_type_adds_plain_query(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Collection types (TPB) add a plain series-name-only query."""
        _series_id, issue_id = await _seed_series_and_issue(db_factory, issue_type=IssueType.TPB)

        captured_queries: list[SearchQuery] = []

        async def _capture_search(query: SearchQuery) -> list[ReleaseResult]:
            captured_queries.append(query)
            return []

        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(side_effect=_capture_search)
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            await svc.search_for_issue(session, issue_id)

        # Should have multiple queries including plain series name
        query_titles = [q.series_title for q in captured_queries]
        assert any(q == "Batman" for q in query_titles)  # Plain series name


# ── SearchService._search_indexers Tests ──────────────────────────────


class TestSearchIndexers:
    """Tests for _search_indexers: fan-out, filtering, and health tracking."""

    @pytest.mark.asyncio
    async def test_category_filter_rejects_non_comic(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Non-comic category results are post-filtered out."""
        r_comic = _make_release("Batman 001.cbz", category="7030")
        r_movie = _make_release("Batman 2022 Movie.mkv", category="2040")

        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(return_value=[r_comic, r_movie])
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        from pullbox.services.search_service import SearchService

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001", issue_number=1.0)
        results = await svc._search_indexers(query)

        assert len(results) == 1
        assert results[0].title == "Batman 001.cbz"

    @pytest.mark.asyncio
    async def test_indexer_error_returns_empty(self) -> None:
        """Indexer search error returns empty results and tracks failure."""
        from pullbox.services.search_service import SearchService

        indexer = AsyncMock()
        indexer.name = "FailingIndexer"
        indexer.search = AsyncMock(side_effect=RuntimeError("Network error"))
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001")
        results = await svc._search_indexers(query)

        assert results == []

    @pytest.mark.asyncio
    async def test_indexer_health_tracking_on_failure(self) -> None:
        """Indexer failure increments failure count and applies backoff at threshold."""
        from pullbox.services.search_service import DEFAULT_INDEXER_FAILURE_THRESHOLD, SearchService

        indexer = AsyncMock()
        indexer.name = "FailingIndexer"
        indexer.search = AsyncMock(side_effect=RuntimeError("Timeout"))
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        # Create a mock config with failure_count just below threshold
        mock_cfg = MagicMock()
        mock_cfg.failure_count = DEFAULT_INDEXER_FAILURE_THRESHOLD - 1
        mock_cfg.disabled_until = None

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001")
        await svc._search_indexers(query, indexer_configs={1: mock_cfg})

        # Should have incremented failure count to threshold and set disabled_until
        assert mock_cfg.failure_count == DEFAULT_INDEXER_FAILURE_THRESHOLD
        assert mock_cfg.disabled_until is not None

    @pytest.mark.asyncio
    async def test_indexer_health_reset_on_success(self) -> None:
        """Successful search resets indexer health fields."""
        from pullbox.services.search_service import SearchService

        r = _make_release("Batman 001.cbz")
        indexer = AsyncMock()
        indexer.name = "GoodIndexer"
        indexer.search = AsyncMock(return_value=[r])
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        mock_cfg = MagicMock()
        mock_cfg.failure_count = 5
        # disabled_until in the PAST — indexer is no longer disabled
        mock_cfg.disabled_until = datetime.now(UTC) - timedelta(hours=1)

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001")
        await svc._search_indexers(query, indexer_configs={1: mock_cfg})

        assert mock_cfg.failure_count == 0
        assert mock_cfg.disabled_until is None

    @pytest.mark.asyncio
    async def test_disabled_indexer_skipped(self) -> None:
        """Indexers with disabled_until in the future are skipped."""
        from pullbox.services.search_service import SearchService

        indexer = AsyncMock()
        indexer.name = "DisabledIndexer"
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        mock_cfg = MagicMock()
        mock_cfg.disabled_until = datetime.now(UTC) + timedelta(hours=1)

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001")
        results = await svc._search_indexers(query, indexer_configs={1: mock_cfg})

        assert results == []
        indexer.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_indexers_returns_empty(self) -> None:
        """No registered indexers returns empty list."""
        from pullbox.services.search_service import SearchService

        registry = MagicMock()
        registry.get_indexer_items.return_value = []

        svc = SearchService(registry)
        query = SearchQuery(series_title="Batman 001")
        results = await svc._search_indexers(query)

        assert results == []


# ── SearchService.search_wanted Tests ─────────────────────────────────


class TestSearchServiceSearchWanted:
    """Tests for SearchService.search_wanted method."""

    @pytest.mark.asyncio
    async def test_search_wanted_returns_results_map(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted returns a mapping of issue_id -> results."""
        _series_id, _issue_ids = await _seed_multiple_wanted(db_factory, count=2)

        r = _make_release("Batman 001 (2016).cbz")
        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(return_value=[r])
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            results_map = await svc.search_wanted(session)

        # Both issues should have results
        assert len(results_map) == 2

    @pytest.mark.asyncio
    async def test_search_wanted_empty_when_no_wanted(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted returns empty map when no WANTED issues exist."""
        registry = MagicMock()
        registry.get_indexer_items.return_value = []

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            results_map = await svc.search_wanted(session)

        assert results_map == {}

    @pytest.mark.asyncio
    async def test_search_wanted_ignores_unmonitored_series(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted only sweeps wanted issues on monitored series."""
        publisher = Publisher(name="Test Publisher")

        monitored_series = Series(
            title="Absolute Superman",
            sort_title="absolute superman",
            year_start=2025,
            monitored=True,
            issue_count=1,
            publisher=publisher,
        )
        unmonitored_series = Series(
            title="Chronicles of Wormwood",
            sort_title="chronicles of wormwood",
            year_start=2007,
            monitored=False,
            issue_count=1,
            publisher=publisher,
        )

        async with db_factory() as session:
            session.add_all([publisher, monitored_series, unmonitored_series])
            await session.flush()
            session.add_all(
                [
                    Issue(
                        series_id=monitored_series.id,
                        issue_number=12.0,
                        title="Issue #12",
                        status=IssueStatus.WANTED,
                        issue_type=IssueType.ISSUE,
                    ),
                    Issue(
                        series_id=unmonitored_series.id,
                        issue_number=1.0,
                        title="Issue #1",
                        status=IssueStatus.WANTED,
                        issue_type=IssueType.ISSUE,
                    ),
                ]
            )
            await session.commit()

        r = _make_release("Absolute Superman 012 (2025).cbz")
        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(return_value=[r])
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            results_map = await svc.search_wanted(session)

        assert len(results_map) == 1
        searched_titles = [call.args[0].series_title for call in indexer.search.await_args_list]
        assert searched_titles == [
            "Absolute Superman 12",
            "Absolute Superman #12",
            "Absolute Superman 012",
            "Absolute Superman #012",
        ]
        assert not any("Unmonitored" in title for title in searched_titles)

    @pytest.mark.asyncio
    async def test_search_wanted_searches_multiple_issues_concurrently(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """search_wanted fans out wanted-issue searches with bounded concurrency."""
        _series_id, _issue_ids = await _seed_multiple_wanted(db_factory, count=3)

        release = _make_release("Batman 001 (2016).cbz")
        active = 0
        max_active = 0

        async def _search(_query: SearchQuery) -> list[ReleaseResult]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return [release]

        indexer = AsyncMock()
        indexer.name = "MockIndexer"
        indexer.search = AsyncMock(side_effect=_search)
        registry = MagicMock()
        registry.get_indexer_items.return_value = [(1, indexer)]

        async with db_factory() as session:
            from pullbox.services.search_service import SearchService

            svc = SearchService(registry)
            results_map = await svc.search_wanted(session)

        assert len(results_map) == 3
        assert max_active > 1


# ── Helper Function Tests ─────────────────────────────────────────────


class TestHelperFunctions:
    """Tests for search_service helper functions."""

    def test_is_comic_category_accepts_7030(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("7030") is True

    def test_is_comic_category_accepts_7020(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("7020") is True

    def test_is_comic_category_rejects_movies(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("2040") is False

    def test_is_comic_category_none_allowed(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category(None) is True

    def test_is_comic_category_empty_allowed(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("") is True

    def test_is_comic_category_named_comics(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("Books/Comics") is True

    def test_is_comic_category_named_non_comic(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("Movies/HD") is False

    def test_is_comic_category_mixed_with_comic(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        assert _is_comic_category("7030,2040") is True

    def test_is_comic_category_prowlarr_extended_ids(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        # Prowlarr 5+ digit IDs are ignored
        assert _is_comic_category("107020") is True

    def test_is_comic_category_prowlarr_extended_only(self) -> None:
        from pullbox.services.search_service import _is_comic_category

        # Only extended IDs → no standard categories → allowed
        assert _is_comic_category("107020,100048") is True

    def test_extract_subtitle_with_colon(self) -> None:
        from pullbox.services.search_service import _extract_subtitle

        result = _extract_subtitle("Vol. 1: All Weather Turns To Storm")
        assert result == "All Weather Turns To Storm"

    def test_extract_subtitle_with_dash(self) -> None:
        from pullbox.services.search_service import _extract_subtitle

        result = _extract_subtitle("Book 2 - The Dark Forest")
        assert result == "The Dark Forest"

    def test_extract_subtitle_no_subtitle(self) -> None:
        from pullbox.services.search_service import _extract_subtitle

        result = _extract_subtitle("Batman")
        assert result is None

    def test_extract_subtitle_single_word(self) -> None:
        from pullbox.services.search_service import _extract_subtitle

        # Single word after colon is not a useful subtitle
        result = _extract_subtitle("Vol. 1: Origins")
        assert result is None

    def test_calculate_backoff(self) -> None:
        from pullbox.services.search_service import (
            DEFAULT_INDEXER_FAILURE_THRESHOLD,
            INDEXER_BACKOFF_SECONDS,
            calculate_backoff,
        )

        threshold = DEFAULT_INDEXER_FAILURE_THRESHOLD
        assert INDEXER_BACKOFF_SECONDS == [900, 3600, 7200]
        # At threshold
        assert calculate_backoff(threshold) == INDEXER_BACKOFF_SECONDS[0]
        # Above threshold
        assert calculate_backoff(threshold + 1) == INDEXER_BACKOFF_SECONDS[1]
        # Way above — caps at last
        assert calculate_backoff(100) == INDEXER_BACKOFF_SECONDS[-1]

    def test_build_type_queries_standard(self) -> None:
        from pullbox.services.search_service import _build_type_queries

        queries = _build_type_queries("Batman", 5.0, IssueType.ISSUE)
        assert queries == ["Batman 5", "Batman 05", "Batman #05", "Batman 005", "Batman #005"]

    def test_build_type_queries_tpb(self) -> None:
        from pullbox.services.search_service import _build_type_queries

        queries = _build_type_queries("Batman", 1.0, IssueType.TPB)
        assert len(queries) >= 2
        assert any("TPB" in q for q in queries)
        assert any("Vol" in q for q in queries)

    def test_build_type_queries_annual(self) -> None:
        from pullbox.services.search_service import _build_type_queries

        queries = _build_type_queries("Batman", 5.0, IssueType.ANNUAL)
        assert any("Annual" in q for q in queries)

    def test_should_auto_grab_high_confidence_issue(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import DEFAULT_TYPE_THRESHOLDS, should_auto_grab

        assert (
            should_auto_grab(MatchConfidence.HIGH, IssueType.ISSUE, DEFAULT_TYPE_THRESHOLDS) is True
        )

    def test_should_auto_grab_medium_confidence_issue(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import DEFAULT_TYPE_THRESHOLDS, should_auto_grab

        assert (
            should_auto_grab(MatchConfidence.MEDIUM, IssueType.ISSUE, DEFAULT_TYPE_THRESHOLDS)
            is True
        )

    def test_should_auto_grab_low_confidence_issue(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import DEFAULT_TYPE_THRESHOLDS, should_auto_grab

        assert (
            should_auto_grab(MatchConfidence.LOW, IssueType.ISSUE, DEFAULT_TYPE_THRESHOLDS) is False
        )

    def test_should_auto_grab_never_threshold(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import should_auto_grab

        thresholds = {"issue": "never"}
        assert should_auto_grab(MatchConfidence.HIGH, IssueType.ISSUE, thresholds) is False

    def test_should_auto_grab_tpb_high(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import DEFAULT_TYPE_THRESHOLDS, should_auto_grab

        assert (
            should_auto_grab(MatchConfidence.HIGH, IssueType.TPB, DEFAULT_TYPE_THRESHOLDS) is True
        )

    def test_should_auto_grab_tpb_medium(self) -> None:
        from pullbox.models.library import MatchConfidence
        from pullbox.services.search_service import DEFAULT_TYPE_THRESHOLDS, should_auto_grab

        # TPB threshold is "high" — medium should NOT auto-grab
        assert (
            should_auto_grab(MatchConfidence.MEDIUM, IssueType.TPB, DEFAULT_TYPE_THRESHOLDS)
            is False
        )

    def test_build_search_details(self) -> None:
        from pullbox.core.release_parser import ParsedRelease
        from pullbox.models.library import MatchConfidence
        from pullbox.services.release_validator import ValidationResult
        from pullbox.services.search_service import build_search_details

        release = _make_release("Batman 001 (2016).cbz")
        parsed = ParsedRelease(
            original_title="Batman 001 (2016).cbz",
            series_name="Batman",
            issue_number=1.0,
            year=2016,
            volume=None,
            issue_type=IssueType.ISSUE,
            scan_group=None,
            file_format="cbz",
            is_pack=False,
            pack_range=None,
        )
        vr = ValidationResult(
            release=release,
            parsed=parsed,
            is_match=True,
            confidence=MatchConfidence.HIGH,
            series_similarity=0.95,
            issue_match=True,
            year_match=True,
            issue_type_match=True,
        )

        details = build_search_details([vr], [], search_time_ms=100, search_passes=1)

        assert details["best_match"] is not None
        assert details["best_match"]["title"] == "Batman 001 (2016).cbz"
        expected_match_fields = {
            "title": "Batman 001 (2016).cbz",
            "indexer": "NZBgeek",
            "confidence": "high",
            "series_similarity": 0.95,
            "status": "matched",
            "parsed_series": "Batman",
            "parsed_issue": 1.0,
            "parsed_year": 2016,
            "issue_type": "issue",
            "rejection_reason": None,
        }
        for key, value in expected_match_fields.items():
            assert details["matched"][0][key] == value
        assert details["rejected_count"] == 0
        assert details["search_passes"] == 1
        assert details["search_time_ms"] == 100
        assert details["type_distribution"] == {"issue": 1}
        assert details["confidence_breakdown"] == {"high": 1}

    def test_build_search_details_with_rejected(self) -> None:
        from pullbox.core.release_parser import ParsedRelease
        from pullbox.models.library import MatchConfidence
        from pullbox.services.release_validator import ValidationResult
        from pullbox.services.search_service import build_search_details

        release = _make_release("Superman 001 (2016).cbz")
        parsed = ParsedRelease(
            original_title="Superman 001 (2016).cbz",
            series_name="Superman",
            issue_number=1.0,
            year=2016,
            volume=None,
            issue_type=IssueType.ISSUE,
            scan_group=None,
            file_format="cbz",
            is_pack=False,
            pack_range=None,
        )
        rejected_vr = ValidationResult(
            release=release,
            parsed=parsed,
            is_match=False,
            confidence=MatchConfidence.LOW,
            series_similarity=0.3,
            issue_match=True,
            year_match=True,
            issue_type_match=True,
            rejection_reason="Wrong series",
        )

        details = build_search_details([], [rejected_vr])

        assert details["best_match"] is None
        assert details["rejected_count"] == 1
        assert len(details["top_rejected"]) == 1
        assert details["top_rejected"][0]["reason"] == "Wrong series"

    def test_sanitize_query(self) -> None:
        from pullbox.services.search_service import _sanitize_query

        assert _sanitize_query("Batman: The Dark Knight") == "Batman The Dark Knight"
        assert _sanitize_query("What If...?") == "What If..."
