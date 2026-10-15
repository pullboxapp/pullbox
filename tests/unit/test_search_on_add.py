"""Unit tests for search_on_add gating in subscribers and search_series_issues.

Tests the subscriber gate (search_on_add=False skips, True triggers search)
and the extracted search_series_issues() helper.

Run:
    pytest tests/unit/test_search_on_add.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-search-on-add-unit")


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_series(
    factory: async_sessionmaker[AsyncSession],
    monitored: bool = True,
    *,
    with_wanted_issues: bool = False,
) -> int:
    """Create a series (optionally with wanted issues) and return its ID."""
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

        if with_wanted_issues:
            for i in range(1, 4):
                issue = Issue(
                    series_id=series_id,
                    comicvine_id=100000 + i,
                    issue_number=float(i),
                    title=f"Issue #{i}",
                    status=IssueStatus.WANTED,
                )
                session.add(issue)

        await session.commit()
        return series_id


# ── Subscriber gating tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscriber_skips_when_search_on_add_false(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_search_new_series returns early when search_on_add is False."""
    from pullbox.core.subscribers import _search_new_series

    series_id = await _seed_series(db_factory, monitored=False)

    event = MagicMock()
    event.series_id = series_id

    with (
        patch("pullbox.core.subscribers.get_session_factory", return_value=db_factory),
        patch("pullbox.core.subscribers.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "pullbox.tasks.search_task.search_series_issues",
            new_callable=AsyncMock,
        ) as mock_search,
    ):
        await _search_new_series(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_subscriber_calls_search_when_search_on_add_true(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_search_new_series delegates to search_series_issues when search_on_add is True."""
    from pullbox.core.subscribers import _search_new_series

    series_id = await _seed_series(db_factory, monitored=True)

    event = MagicMock()
    event.series_id = series_id

    with (
        patch("pullbox.core.subscribers.get_session_factory", return_value=db_factory),
        patch("pullbox.core.subscribers.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "pullbox.tasks.search_task.search_series_issues",
            new_callable=AsyncMock,
            return_value={"wanted": 3, "sent": 1},
        ) as mock_search,
    ):
        await _search_new_series(event)

    mock_search.assert_called_once_with(series_id)


@pytest.mark.asyncio
async def test_subscriber_defers_search_while_catalog_hydrating(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_search_new_series waits until targeted imports finish catalog hydration."""
    from pullbox.core.subscribers import _search_new_series

    series_id = await _seed_series(db_factory, monitored=True)
    async with db_factory() as session:
        series = await session.get(Series, series_id)
        assert series is not None
        series.issue_catalog_state = IssueCatalogState.HYDRATING
        await session.commit()

    event = MagicMock()
    event.series_id = series_id

    with (
        patch("pullbox.core.subscribers.get_session_factory", return_value=db_factory),
        patch("pullbox.core.subscribers.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "pullbox.tasks.search_task.search_series_issues",
            new_callable=AsyncMock,
        ) as mock_search,
    ):
        await _search_new_series(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_subscriber_handles_missing_series(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_search_new_series returns early when series does not exist."""
    from pullbox.core.subscribers import _search_new_series

    event = MagicMock()
    event.series_id = 99999  # doesn't exist

    with (
        patch("pullbox.core.subscribers.get_session_factory", return_value=db_factory),
        patch("pullbox.core.subscribers.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "pullbox.tasks.search_task.search_series_issues",
            new_callable=AsyncMock,
        ) as mock_search,
    ):
        await _search_new_series(event)

    mock_search.assert_not_called()


# ── search_series_issues tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_search_series_issues_no_indexers(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues returns early when no indexers are configured."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=True)

    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await search_series_issues(series_id)

    assert result == {"wanted": 0, "sent": 0, "queued": 0}


@pytest.mark.asyncio
async def test_search_series_issues_no_wanted(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues returns early when no issues are WANTED."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=False)

    mock_registry = MagicMock()
    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
    ):
        result = await search_series_issues(series_id)

    assert result == {"wanted": 0, "sent": 0, "queued": 0}


@pytest.mark.asyncio
async def test_search_series_issues_not_found(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues returns early when series doesn't exist."""
    from pullbox.tasks.search_task import search_series_issues

    mock_registry = MagicMock()
    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
    ):
        result = await search_series_issues(99999)

    assert result == {"wanted": 0, "sent": 0, "queued": 0}


@pytest.mark.asyncio
async def test_search_series_issues_finds_and_sends(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues searches wanted issues and sends best matches."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=True)

    mock_registry = MagicMock()
    mock_release = MagicMock()
    mock_release.title = "Test Series #1"
    mock_release.indexer_name = "TestIndexer"

    mock_search_svc = MagicMock()
    mock_search_svc.search_for_issue = AsyncMock(return_value=[mock_release])
    mock_search_svc.evaluate_results = MagicMock(return_value=mock_release)

    mock_download_svc = MagicMock()
    mock_download_svc.send_to_client = AsyncMock()

    # Mock validation to return HIGH confidence (→ auto-grab)
    from pullbox.models.library import MatchConfidence

    mock_validation = MagicMock()
    mock_validation.confidence = MatchConfidence.HIGH

    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.tasks.search_task.SearchService",
            return_value=mock_search_svc,
        ),
        patch(
            "pullbox.tasks.search_task.DownloadService",
            return_value=mock_download_svc,
        ),
        patch(
            "pullbox.tasks.search_task.InterventionService",
        ),
        patch(
            "pullbox.tasks.search_task.build_eval_kwargs",
            return_value={},
        ),
        patch("pullbox.tasks.search_task.ReleaseValidator") as mock_validator_cls,
    ):
        mock_validator_cls.return_value.validate_results = MagicMock(return_value=[mock_validation])
        result = await search_series_issues(series_id)

    assert result["wanted"] == 3
    assert result["sent"] == 3
    assert mock_search_svc.search_for_issue.call_count == 3
    assert mock_download_svc.send_to_client.call_count == 3


@pytest.mark.asyncio
async def test_search_series_issues_no_match_found(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues handles no matching results gracefully."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=True)

    mock_registry = MagicMock()
    mock_search_svc = MagicMock()
    mock_search_svc.search_for_issue = AsyncMock(return_value=[MagicMock()])
    mock_search_svc.evaluate_results = MagicMock(return_value=None)  # no match

    mock_download_svc = MagicMock()
    mock_download_svc.send_to_client = AsyncMock()

    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.tasks.search_task.SearchService",
            return_value=mock_search_svc,
        ),
        patch(
            "pullbox.tasks.search_task.DownloadService",
            return_value=mock_download_svc,
        ),
        patch("pullbox.tasks.search_task.InterventionService"),
        patch(
            "pullbox.tasks.search_task.build_eval_kwargs",
            return_value={},
        ),
    ):
        result = await search_series_issues(series_id)

    assert result["wanted"] == 3
    assert result["sent"] == 0
    mock_download_svc.send_to_client.assert_not_called()


@pytest.mark.asyncio
async def test_search_series_issues_empty_results(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues handles empty search results per issue."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=True)

    mock_registry = MagicMock()
    mock_search_svc = MagicMock()
    mock_search_svc.search_for_issue = AsyncMock(return_value=[])  # no results

    mock_download_svc = MagicMock()

    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.tasks.search_task.SearchService",
            return_value=mock_search_svc,
        ),
        patch(
            "pullbox.tasks.search_task.DownloadService",
            return_value=mock_download_svc,
        ),
        patch("pullbox.tasks.search_task.InterventionService"),
        patch(
            "pullbox.tasks.search_task.build_eval_kwargs",
            return_value={},
        ),
    ):
        result = await search_series_issues(series_id)

    assert result["wanted"] == 3
    assert result["sent"] == 0


@pytest.mark.asyncio
async def test_search_series_issues_send_failure(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """search_series_issues handles send_to_client failure without crashing."""
    from pullbox.tasks.search_task import search_series_issues

    series_id = await _seed_series(db_factory, monitored=True, with_wanted_issues=True)

    mock_registry = MagicMock()
    mock_release = MagicMock()
    mock_release.title = "Test Series #1"
    mock_release.indexer_name = "TestIndexer"

    mock_search_svc = MagicMock()
    mock_search_svc.search_for_issue = AsyncMock(return_value=[mock_release])
    mock_search_svc.evaluate_results = MagicMock(return_value=mock_release)

    mock_download_svc = MagicMock()
    mock_download_svc.send_to_client = AsyncMock(side_effect=RuntimeError("send failed"))

    # Mock validation to return HIGH confidence (→ auto-grab)
    from pullbox.models.library import MatchConfidence

    mock_validation = MagicMock()
    mock_validation.confidence = MatchConfidence.HIGH

    with (
        patch("pullbox.tasks.search_task.get_session_factory", return_value=db_factory),
        patch(
            "pullbox.tasks.search_task.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.tasks.search_task.SearchService",
            return_value=mock_search_svc,
        ),
        patch(
            "pullbox.tasks.search_task.DownloadService",
            return_value=mock_download_svc,
        ),
        patch(
            "pullbox.tasks.search_task.InterventionService",
        ),
        patch(
            "pullbox.tasks.search_task.build_eval_kwargs",
            return_value={},
        ),
        patch("pullbox.tasks.search_task.ReleaseValidator") as mock_validator_cls,
    ):
        mock_validator_cls.return_value.validate_results = MagicMock(return_value=[mock_validation])
        result = await search_series_issues(series_id)

    # All 3 attempted but none succeeded
    assert result["wanted"] == 3
    assert result["sent"] == 0


# ── SeriesService.add_from_comicvine sets search_on_add ──────────────


@pytest.mark.asyncio
async def test_add_from_comicvine_search_on_add_sets_monitored(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """add_from_comicvine with search_on_add=True sets monitored=True on the series."""
    from pullbox.core.events import EventBus
    from pullbox.providers.base import SeriesMetadata
    from pullbox.services.series_service import SeriesService

    mock_metadata = MagicMock()

    async def fake_upsert_series(
        session: AsyncSession,
        cv_id: int,
        meta: SeriesMetadata,
    ) -> Series:
        series = Series(
            comicvine_id=cv_id,
            title=meta.title,
            sort_title=meta.sort_title,
            year_start=meta.year_start,
            status=SeriesStatus.CONTINUING,
        )
        session.add(series)
        await session.flush()
        return series

    mock_metadata.get_series_metadata = AsyncMock(
        return_value=SeriesMetadata(
            provider_id="12345",
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            year_end=None,
            status="continuing",
            publisher=None,
            description=None,
            cover_url=None,
            issue_count=0,
            comicvine_url=None,
        )
    )
    mock_metadata.get_issue_summaries_for_series = AsyncMock(return_value=[])
    mock_metadata.upsert_series_metadata = AsyncMock(side_effect=fake_upsert_series)
    mock_metadata.classify_and_link_series = AsyncMock()
    mock_metadata.upsert_issue_summaries = AsyncMock(return_value=[])
    mock_metadata.infer_series_status = AsyncMock()

    event_bus = EventBus()
    svc = SeriesService(metadata_service=mock_metadata, event_bus=event_bus)

    async with db_factory() as session:
        series = await svc.add_from_comicvine(
            session,
            comicvine_id=12345,
            search_on_add=True,
        )
        assert series.monitored is True
        await session.commit()

    # Verify it persisted
    async with db_factory() as session:
        result = await session.execute(select(Series).where(Series.id == series.id))
        reloaded = result.scalar_one()
        assert reloaded.monitored is True


@pytest.mark.asyncio
async def test_add_from_comicvine_search_on_add_default_false(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """add_from_comicvine defaults search_on_add to False, so monitored stays False."""
    from pullbox.core.events import EventBus
    from pullbox.providers.base import SeriesMetadata
    from pullbox.services.series_service import SeriesService

    mock_metadata = MagicMock()

    async def fake_upsert_series(
        session: AsyncSession,
        cv_id: int,
        meta: SeriesMetadata,
    ) -> Series:
        series = Series(
            comicvine_id=cv_id,
            title=meta.title,
            sort_title=meta.sort_title,
            year_start=meta.year_start,
            status=SeriesStatus.CONTINUING,
        )
        session.add(series)
        await session.flush()
        return series

    mock_metadata.get_series_metadata = AsyncMock(
        return_value=SeriesMetadata(
            provider_id="54321",
            title="Spider-Man",
            sort_title="Spider-Man",
            year_start=2018,
            year_end=None,
            status="continuing",
            publisher=None,
            description=None,
            cover_url=None,
            issue_count=0,
            comicvine_url=None,
        )
    )
    mock_metadata.get_issue_summaries_for_series = AsyncMock(return_value=[])
    mock_metadata.upsert_series_metadata = AsyncMock(side_effect=fake_upsert_series)
    mock_metadata.classify_and_link_series = AsyncMock()
    mock_metadata.upsert_issue_summaries = AsyncMock(return_value=[])
    mock_metadata.infer_series_status = AsyncMock()

    event_bus = EventBus()
    svc = SeriesService(metadata_service=mock_metadata, event_bus=event_bus)

    async with db_factory() as session:
        series = await svc.add_from_comicvine(
            session,
            comicvine_id=54321,
        )
        assert series.monitored is False
