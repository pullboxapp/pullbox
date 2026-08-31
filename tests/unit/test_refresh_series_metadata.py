"""Unit tests for refresh_series metadata workflow.

Tests that refresh_series correctly fetches both series metadata AND
issues from ComicVine, and respects staleness checks.

Run:
    pytest tests/unit/test_refresh_series_metadata.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.exceptions import NotFoundError, ProviderError
from pullbox.models import Base
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import Series, SeriesStatus, SeriesStatusOverride, SeriesType
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.services.metadata_service import MetadataService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-refresh")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory DB with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def series_with_cv(
    db_factory: async_sessionmaker[AsyncSession],
) -> Series:
    """Create a series with a ComicVine ID."""
    async with db_factory() as session:
        series = Series(
            title="Absolute Superman",
            sort_title="Absolute Superman",
            year_start=2025,
            comicvine_id=160860,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            metadata_last_refreshed=datetime.now(UTC) - timedelta(days=30),
        )
        session.add(series)
        await session.commit()
        await session.refresh(series)
        return series


@pytest.fixture
async def series_no_cv(
    db_factory: async_sessionmaker[AsyncSession],
) -> Series:
    """Create a series without a ComicVine ID."""
    async with db_factory() as session:
        series = Series(
            title="Unknown Series",
            sort_title="Unknown Series",
            year_start=2025,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            comicvine_id=None,
        )
        session.add(series)
        await session.commit()
        await session.refresh(series)
        return series


@pytest.fixture
def metadata_svc() -> MetadataService:
    """MetadataService with mocked provider."""
    provider = MagicMock()
    svc = MetadataService(provider=provider, covers_dir=MagicMock())
    svc.fetch_series = AsyncMock()
    svc.fetch_issues_for_series = AsyncMock()
    svc.infer_series_status = AsyncMock()
    return svc


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_fetch_wrappers_return_provider_results() -> None:
    provider = MagicMock()
    series_meta = SeriesMetadata(
        provider_id="123",
        title="Test Series",
        sort_title="Test Series",
        year_start=2026,
        year_end=None,
        status="continuing",
        publisher="Test Publisher",
        description="Series description",
        cover_url=None,
        issue_count=2,
        comicvine_url="https://comicvine.gamespot.com/test/4050-123/",
    )
    issue_summary = IssueSummary(
        provider_id="456",
        issue_number=1.0,
        title="Issue One",
        release_date="2026-06-01",
        cover_url=None,
        issue_type="issue",
    )
    provider.get_series = AsyncMock(return_value=series_meta)
    provider.get_issues_for_series = AsyncMock(return_value=[issue_summary])
    provider.get_recent_issues_for_series = AsyncMock(return_value=[issue_summary])
    service = MetadataService(provider=provider, covers_dir=MagicMock())

    assert await service.get_series_metadata(123) == series_meta
    assert await service.get_issue_summaries_for_series(123) == [issue_summary]
    assert await service.get_recent_issue_summaries_for_series(123, limit=5) == [issue_summary]

    provider.get_series.assert_awaited_once_with("123")
    provider.get_issues_for_series.assert_awaited_once_with("123")
    provider.get_recent_issues_for_series.assert_awaited_once_with("123", limit=5)


@pytest.mark.asyncio
async def test_full_and_recent_issue_fetches_use_different_consensus_policies(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())
    service.get_issue_summaries_for_series = AsyncMock(return_value=[])
    service.get_recent_issue_summaries_for_series = AsyncMock(return_value=[])
    service.upsert_issue_summaries = AsyncMock(return_value=[])

    async with db_factory() as session:
        series = Series(
            title="Spawn",
            sort_title="Spawn",
            comicvine_id=12345,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
        )
        session.add(series)
        await session.flush()

        await service.fetch_issues_for_series(session, series.id)
        full_call = service.upsert_issue_summaries.await_args
        await service.fetch_recent_issues_for_series(session, series.id)
        recent_call = service.upsert_issue_summaries.await_args

    assert full_call.kwargs == {"infer_series_type_from_summaries": True}
    assert recent_call.kwargs == {"infer_series_type_from_summaries": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("get_series_metadata", (123,)),
        ("get_issue_summaries_for_series", (123,)),
        ("get_recent_issue_summaries_for_series", (123,)),
    ],
)
async def test_metadata_fetch_wrappers_translate_comicvine_errors(
    method_name: str,
    args: tuple[int, ...],
) -> None:
    provider = MagicMock()
    provider.get_series = AsyncMock(side_effect=ComicVineError(107, "rate limited"))
    provider.get_issues_for_series = AsyncMock(side_effect=ComicVineError(107, "rate limited"))
    provider.get_recent_issues_for_series = AsyncMock(
        side_effect=ComicVineError(107, "rate limited")
    )
    service = MetadataService(provider=provider, covers_dir=MagicMock())

    with pytest.raises(ProviderError, match="rate limited"):
        await getattr(service, method_name)(*args)


@pytest.mark.asyncio
async def test_fetch_issue_preserves_retryable_provider_details() -> None:
    provider = MagicMock()
    provider.get_issue = AsyncMock(
        side_effect=ComicVineError(420, "HTTP 420: /issue/4000-123/", retryable=True)
    )
    service = MetadataService(provider=provider, covers_dir=MagicMock())

    with pytest.raises(ProviderError, match="HTTP 420") as exc_info:
        await service.fetch_issue(MagicMock(), 123)

    assert exc_info.value.details == {"status_code": 420, "retryable": True}


class TestRefreshSeries:
    """Tests for MetadataService.refresh_series."""

    @pytest.mark.asyncio
    async def test_refresh_calls_fetch_series_and_fetch_issues(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_with_cv: Series,
        metadata_svc: MetadataService,
    ) -> None:
        """refresh_series must call both fetch_series and fetch_issues_for_series."""
        metadata_svc.fetch_series.return_value = series_with_cv

        async with db_factory() as session:
            await metadata_svc.refresh_series(session, series_with_cv.id, force=True)

        metadata_svc.fetch_series.assert_awaited_once_with(session, 160860)
        metadata_svc.fetch_issues_for_series.assert_awaited_once_with(session, series_with_cv.id)
        metadata_svc.infer_series_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_preserves_existing_years_when_provider_returns_null(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Hydration should not turn import/search year metadata into Unknown."""
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=171785,
                title="The Bat-Man: Second Knight",
                sort_title="The Bat-Man: Second Knight",
                year_start=2026,
                year_end=2026,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.HARDCOVER,
                issue_count=1,
                metadata_source="comicvine_partial",
            )
            session.add(series)
            await session.flush()

            updated = await service.upsert_series_metadata(
                session,
                171785,
                SeriesMetadata(
                    provider_id="171785",
                    title="The Bat-Man: Second Knight",
                    sort_title="The Bat-Man: Second Knight",
                    year_start=None,
                    year_end=None,
                    status=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=1,
                    comicvine_url="https://comicvine.gamespot.com/the-bat-man-second-knight/4050-171785/",
                ),
            )

            assert updated.year_start == 2026
            assert updated.year_end == 2026
            assert updated.metadata_source == "comicvine"

    @pytest.mark.asyncio
    async def test_partial_upsert_preserves_existing_classification_evidence(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=22222,
                title="Batman: Officer Down",
                sort_title="Batman: Officer Down",
                year_start=2001,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.VOLUME,
                issue_count=1,
                description="This collection includes stories from Batman #587-590.",
                metadata_source="comicvine",
            )
            session.add(series)
            await session.flush()

            updated = await service.upsert_series_metadata(
                session,
                22222,
                SeriesMetadata(
                    provider_id="22222",
                    title="Batman: Officer Down",
                    sort_title="Batman: Officer Down",
                    year_start=2001,
                    year_end=None,
                    status=None,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=None,
                    comicvine_url=None,
                ),
            )

            assert updated.description == ("This collection includes stories from Batman #587-590.")
            assert updated.issue_count == 1
            assert updated.series_type == SeriesType.VOLUME

    @pytest.mark.asyncio
    async def test_upsert_preserves_manual_status_override(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ComicVine refreshes must not replace a user-owned lifecycle status."""
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=171785,
                title="Manual Status",
                sort_title="Manual Status",
                year_start=2025,
                status=SeriesStatus.ENDED,
                status_override=SeriesStatusOverride.ENDED,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            updated = await service.upsert_series_metadata(
                session,
                171785,
                SeriesMetadata(
                    provider_id="171785",
                    title="Manual Status",
                    sort_title="Manual Status",
                    year_start=2025,
                    year_end=None,
                    status=SeriesStatus.CONTINUING.value,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=1,
                    comicvine_url="https://comicvine.gamespot.com/manual-status/4050-171785/",
                ),
            )

            assert updated.status == SeriesStatus.ENDED
            assert updated.status_override == SeriesStatusOverride.ENDED

    @pytest.mark.asyncio
    async def test_upsert_reclassifies_series_and_repairs_only_inherited_issue_types(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=12345,
                title="Spawn",
                sort_title="Spawn",
                year_start=1992,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.SPECIAL,
                issue_count=2,
                description="An ongoing series about Al Simmons.",
                metadata_source="comicvine",
            )
            session.add(series)
            await session.flush()
            ordinary = Issue(
                series_id=series.id,
                comicvine_id=1001,
                issue_number=1.0,
                title="Questions",
                issue_type=IssueType.SPECIAL,
                status=IssueStatus.SKIPPED,
                metadata_source="comicvine",
            )
            actual_special = Issue(
                series_id=series.id,
                comicvine_id=1002,
                issue_number=2.0,
                title="Holiday Special",
                issue_type=IssueType.SPECIAL,
                status=IssueStatus.SKIPPED,
                metadata_source="comicvine",
            )
            session.add_all([ordinary, actual_special])
            await session.flush()

            updated = await service.upsert_series_metadata(
                session,
                12345,
                SeriesMetadata(
                    provider_id="12345",
                    title="Spawn",
                    sort_title="Spawn",
                    year_start=1992,
                    year_end=2026,
                    status=SeriesStatus.ENDED.value,
                    publisher=None,
                    description="An ongoing series about Al Simmons.",
                    cover_url=None,
                    issue_count=2,
                    comicvine_url="https://comicvine.gamespot.com/spawn/4050-12345/",
                ),
            )

            assert updated.series_type == SeriesType.STANDARD
            assert ordinary.issue_type == IssueType.ISSUE
            assert actual_special.issue_type == IssueType.SPECIAL

    @pytest.mark.asyncio
    async def test_upsert_preserves_explicit_provisional_issue_type_during_reclassification(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=54321,
                title="Imported Collection",
                sort_title="Imported Collection",
                year_start=2020,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.ONE_SHOT,
                issue_count=1,
                metadata_source="comicvine_partial",
            )
            session.add(series)
            await session.flush()
            provisional = Issue(
                series_id=series.id,
                issue_number=1.0,
                title=None,
                issue_type=IssueType.TPB,
                status=IssueStatus.OWNED,
                metadata_source="provisional_import",
            )
            session.add(provisional)
            await session.flush()

            await service.upsert_series_metadata(
                session,
                54321,
                SeriesMetadata(
                    provider_id="54321",
                    title="Imported Collection",
                    sort_title="Imported Collection",
                    year_start=2020,
                    year_end=2020,
                    status=SeriesStatus.ENDED.value,
                    publisher=None,
                    description="A story without format metadata.",
                    cover_url=None,
                    issue_count=1,
                    comicvine_url=None,
                ),
            )

            assert series.series_type == SeriesType.STANDARD
            assert provisional.issue_type == IssueType.TPB
            assert provisional.metadata_source == "provisional_import"

    @pytest.mark.asyncio
    async def test_upsert_preserves_credible_collection_type_without_new_evidence(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=88888,
                title="The Chair",
                sort_title="The Chair",
                status=SeriesStatus.ENDED,
                series_type=SeriesType.GRAPHIC_NOVEL,
                issue_count=1,
                metadata_source="comicvine",
            )
            session.add(series)
            await session.flush()
            issue = Issue(
                series_id=series.id,
                comicvine_id=99999,
                issue_number=1.0,
                title=None,
                issue_type=IssueType.GN,
                status=IssueStatus.SKIPPED,
                metadata_source="comicvine",
            )
            session.add(issue)
            await session.flush()

            await service.upsert_series_metadata(
                session,
                88888,
                SeriesMetadata(
                    provider_id="88888",
                    title="The Chair",
                    sort_title="The Chair",
                    year_start=2008,
                    year_end=2008,
                    status=SeriesStatus.ENDED.value,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=1,
                    comicvine_url=None,
                ),
            )

            assert series.series_type == SeriesType.GRAPHIC_NOVEL
            assert issue.issue_type == IssueType.GN

    @pytest.mark.asyncio
    async def test_upsert_uses_complete_existing_issue_title_consensus(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=77777,
                title="Collected Stories",
                sort_title="Collected Stories",
                status=SeriesStatus.ENDED,
                series_type=SeriesType.TPB,
                issue_count=2,
                metadata_source="comicvine",
            )
            session.add(series)
            await session.flush()
            issues = [
                Issue(
                    series_id=series.id,
                    comicvine_id=70000 + number,
                    issue_number=float(number),
                    title=f"Volume {number}",
                    issue_type=IssueType.TPB,
                    status=IssueStatus.SKIPPED,
                    metadata_source="comicvine",
                )
                for number in (1, 2)
            ]
            session.add_all(issues)
            await session.flush()

            await service.upsert_series_metadata(
                session,
                77777,
                SeriesMetadata(
                    provider_id="77777",
                    title="Collected Stories",
                    sort_title="Collected Stories",
                    year_start=2020,
                    year_end=2021,
                    status=SeriesStatus.ENDED.value,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=2,
                    comicvine_url=None,
                ),
            )

            assert series.series_type == SeriesType.VOLUME
            assert [issue.issue_type for issue in issues] == [
                IssueType.VOLUME,
                IssueType.VOLUME,
            ]

    @pytest.mark.asyncio
    async def test_upsert_continuing_override_rejects_provider_end_year(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Provider refreshes must not reintroduce an end year for a continuing override."""
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                comicvine_id=171785,
                title="Still Continuing",
                sort_title="Still Continuing",
                year_start=2025,
                year_end=None,
                status=SeriesStatus.CONTINUING,
                status_override=SeriesStatusOverride.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            updated = await service.upsert_series_metadata(
                session,
                171785,
                SeriesMetadata(
                    provider_id="171785",
                    title="Still Continuing",
                    sort_title="Still Continuing",
                    year_start=2025,
                    year_end=2026,
                    status=SeriesStatus.ENDED.value,
                    publisher=None,
                    description=None,
                    cover_url=None,
                    issue_count=12,
                    comicvine_url="https://comicvine.gamespot.com/still-continuing/4050-171785/",
                ),
            )

            assert updated.status == SeriesStatus.CONTINUING
            assert updated.status_override == SeriesStatusOverride.CONTINUING
            assert updated.year_end is None

    @pytest.mark.asyncio
    async def test_refresh_without_force_skips_when_fresh(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        metadata_svc: MetadataService,
    ) -> None:
        """refresh_series skips fetch when metadata is fresh and force=False."""
        async with db_factory() as session:
            series = Series(
                title="Fresh Series",
                sort_title="Fresh Series",
                year_start=2025,
                comicvine_id=12345,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                metadata_last_refreshed=datetime.now(UTC),
            )
            session.add(series)
            await session.commit()
            await session.refresh(series)

            result = await metadata_svc.refresh_series(session, series.id, force=False)

        assert result.title == "Fresh Series"
        metadata_svc.fetch_series.assert_not_awaited()
        metadata_svc.fetch_issues_for_series.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_with_force_ignores_freshness(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        metadata_svc: MetadataService,
    ) -> None:
        """refresh_series with force=True fetches even when metadata is fresh."""
        async with db_factory() as session:
            series = Series(
                title="Fresh Series",
                sort_title="Fresh Series",
                year_start=2025,
                comicvine_id=12345,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                metadata_last_refreshed=datetime.now(UTC),
            )
            session.add(series)
            await session.commit()
            await session.refresh(series)

            metadata_svc.fetch_series.return_value = series
            await metadata_svc.refresh_series(session, series.id, force=True)

        metadata_svc.fetch_series.assert_awaited_once()
        metadata_svc.fetch_issues_for_series.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_raises_not_found_for_missing_series(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        metadata_svc: MetadataService,
    ) -> None:
        """refresh_series raises NotFoundError for nonexistent series ID."""
        async with db_factory() as session:
            with pytest.raises(NotFoundError, match="Series"):
                await metadata_svc.refresh_series(session, 99999, force=True)

    @pytest.mark.asyncio
    async def test_refresh_raises_provider_error_without_comicvine_id(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_no_cv: Series,
        metadata_svc: MetadataService,
    ) -> None:
        """refresh_series raises ProviderError when series has no ComicVine ID."""
        async with db_factory() as session:
            with pytest.raises(ProviderError, match="no ComicVine ID"):
                await metadata_svc.refresh_series(session, series_no_cv.id, force=True)

        metadata_svc.fetch_series.assert_not_awaited()
        metadata_svc.fetch_issues_for_series.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_calls_infer_status_after_issue_fetch(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        series_with_cv: Series,
        metadata_svc: MetadataService,
    ) -> None:
        """infer_series_status is called after both series and issue fetch complete."""
        call_order: list[str] = []
        metadata_svc.fetch_series.return_value = series_with_cv
        metadata_svc.fetch_series.side_effect = lambda *a, **kw: (
            call_order.append("fetch_series") or series_with_cv
        )
        metadata_svc.fetch_issues_for_series.side_effect = lambda *a, **kw: call_order.append(
            "fetch_issues"
        )
        metadata_svc.infer_series_status.side_effect = lambda *a, **kw: call_order.append(
            "infer_status"
        )

        async with db_factory() as session:
            await metadata_svc.refresh_series(session, series_with_cv.id, force=True)

        assert call_order == ["fetch_series", "fetch_issues", "infer_status"]

    @pytest.mark.asyncio
    async def test_fetch_series_clears_stale_cover_cache_for_new_series(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A newly created series must not inherit stale cached art for a reused id."""
        covers_dir = tmp_path / ".covers"
        stale_cover_dir = covers_dir / "1"
        stale_cover_dir.mkdir(parents=True)
        (stale_cover_dir / "series.jpg").write_bytes(b"stale-cover")

        provider = MagicMock()
        provider.get_series = AsyncMock(
            return_value=SimpleNamespace(
                title="Absolute Martian Manhunter",
                sort_title="Absolute Martian Manhunter",
                year_start=2025,
                year_end=None,
                status=SeriesStatus.CONTINUING.value,
                description="A new series",
                issue_count=1,
                comicvine_url="https://example.com/series/160860",
                cover_url="https://example.com/cover.jpg",
                publisher=None,
            )
        )
        service = MetadataService(provider=provider, covers_dir=covers_dir)
        settings = SimpleNamespace(covers_dir=covers_dir)

        with (
            patch("pullbox.services.cover_resolver.get_settings", return_value=settings),
            patch("pullbox.services.cover_cache_service.get_settings", return_value=settings),
        ):
            async with db_factory() as session:
                series = await service.fetch_series(session, 160860, download_cover=False)

        assert series.id == 1
        assert series.cover_path is None
        assert not stale_cover_dir.exists()


class TestInferSeriesStatus:
    """Tests for status/year_end inference on series metadata."""

    @pytest.mark.asyncio
    async def test_nonstandard_series_derives_end_year_from_latest_issue_date(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Thanos: The Infinity Revelation",
                sort_title="Thanos: The Infinity Revelation",
                year_start=2014,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.HARDCOVER,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    release_date=date(2014, 8, 27),
                    status=IssueStatus.OWNED,
                )
            )
            await session.flush()

            await service.infer_series_status(session, series)

            assert series.status == SeriesStatus.ENDED
            assert series.year_end == 2014

    @pytest.mark.asyncio
    async def test_inference_preserves_manual_continuing_override(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Issue-date inference must not replace a manual lifecycle override."""
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())
        old_release = date.today() - timedelta(days=365)

        async with db_factory() as session:
            series = Series(
                title="Still Going",
                sort_title="Still Going",
                year_start=2020,
                year_end=2024,
                status=SeriesStatus.CONTINUING,
                status_override=SeriesStatusOverride.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    release_date=old_release,
                    status=IssueStatus.OWNED,
                )
            )
            await session.flush()

            await service.infer_series_status(session, series)

            assert series.status == SeriesStatus.CONTINUING
            assert series.status_override == SeriesStatusOverride.CONTINUING
            assert series.year_end is None

    @pytest.mark.asyncio
    async def test_nonstandard_series_without_issue_dates_falls_back_to_start_year(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Original Graphic Novel",
                sort_title="Original Graphic Novel",
                year_start=2020,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.GRAPHIC_NOVEL,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    status=IssueStatus.OWNED,
                )
            )
            await session.flush()

            await service.infer_series_status(session, series)

            assert series.status == SeriesStatus.ENDED
            assert series.year_end == 2020

    @pytest.mark.asyncio
    async def test_standard_continuing_series_clears_stale_end_year(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Batman",
                sort_title="Batman",
                year_start=2024,
                year_end=2024,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    release_date=date.today(),
                    status=IssueStatus.OWNED,
                )
            )
            await session.flush()

            await service.infer_series_status(session, series)

            assert series.status == SeriesStatus.CONTINUING
            assert series.year_end is None

    @pytest.mark.asyncio
    async def test_standard_ended_series_sets_year_end_from_latest_release(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())
        old_release = date.today() - timedelta(days=365)

        async with db_factory() as session:
            series = Series(
                title="John Carpenter's Tales of Science Fiction: Redhead",
                sort_title="John Carpenter's Tales of Science Fiction: Redhead",
                year_start=2019,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    release_date=old_release,
                    status=IssueStatus.OWNED,
                )
            )
            await session.flush()

            await service.infer_series_status(session, series)

            assert series.status == SeriesStatus.ENDED
            assert series.year_end == old_release.year


class TestUpsertIssueSummaries:
    """Tests for issue summary sync behavior."""

    @pytest.mark.asyncio
    async def test_upsert_issue_summaries_attaches_comicvine_id_to_provisional_issue(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="King Dracula",
                sort_title="King Dracula",
                year_start=2025,
                comicvine_id=169964,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()
            provisional = Issue(
                series_id=series.id,
                issue_number=4.0,
                comicvine_id=None,
                title=None,
                status=IssueStatus.OWNED,
                metadata_source="provisional_import",
            )
            session.add(provisional)
            await session.flush()

            await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="1170004",
                        issue_number=4.0,
                        title="Final Sacrifice",
                        release_date="2026-04-22",
                        cover_url=None,
                        issue_type="issue",
                    )
                ],
            )

            await session.refresh(provisional)
            assert provisional.comicvine_id == 1170004
            assert provisional.issue_number_text == "4"
            assert provisional.title == "Final Sacrifice"
            assert provisional.metadata_source == "comicvine"

    @pytest.mark.asyncio
    async def test_upsert_issue_summaries_dual_writes_provider_issue_number_text(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Suffix Issue",
                sort_title="Suffix Issue",
                comicvine_id=170001,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            created = await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="12001",
                        issue_number=1.0,
                        title="After Universe",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                        issue_number_text="1au",
                    )
                ],
            )

            assert len(created) == 1
            assert created[0].issue_number == 1.0
            assert created[0].issue_number_text == "1AU"

    @pytest.mark.asyncio
    async def test_upsert_issue_summaries_honors_explicit_issue_type(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="AL15",
                sort_title="AL15",
                year_start=2021,
                comicvine_id=136732,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            created = await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="1149277",
                        issue_number=2.0,
                        title="Broken Dreams",
                        release_date=None,
                        cover_url=None,
                        issue_type=IssueType.VOLUME.value,
                    )
                ],
            )

            assert len(created) == 1
            assert created[0].issue_type == IssueType.VOLUME

    @pytest.mark.asyncio
    async def test_full_catalog_mixed_standard_and_special_does_not_retype_series(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="MAD Magazine",
                sort_title="MAD Magazine",
                comicvine_id=9318,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            created = await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="1",
                        issue_number=1.0,
                        title="Issue One",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    ),
                    IssueSummary(
                        provider_id="2",
                        issue_number=2.0,
                        title="Anniversary Special",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    ),
                ],
                infer_series_type_from_summaries=True,
            )

            assert series.series_type == SeriesType.STANDARD
            assert [issue.issue_type for issue in created] == [
                IssueType.ISSUE,
                IssueType.SPECIAL,
            ]

    @pytest.mark.asyncio
    async def test_full_catalog_mixed_collection_bindings_infers_generic_volume(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Collected Stories",
                sort_title="Collected Stories",
                comicvine_id=76543,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="11",
                        issue_number=1.0,
                        title="TPB",
                        release_date=None,
                        cover_url=None,
                        issue_type=IssueType.TPB.value,
                    ),
                    IssueSummary(
                        provider_id="12",
                        issue_number=2.0,
                        title="HC",
                        release_date=None,
                        cover_url=None,
                        issue_type=IssueType.HC.value,
                    ),
                ],
                infer_series_type_from_summaries=True,
            )

            assert series.series_type == SeriesType.VOLUME

    @pytest.mark.asyncio
    async def test_partial_summary_subset_cannot_retype_series(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Spawn",
                sort_title="Spawn",
                comicvine_id=12345,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()

            await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="99",
                        issue_number=100.0,
                        title="Anniversary Special",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    )
                ],
            )

            assert series.series_type == SeriesType.STANDARD

    @pytest.mark.asyncio
    async def test_upsert_issue_summaries_reconciles_existing_provider_id_issue_number(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Wolverine and Black Cat: Claws II",
                sort_title="Wolverine and Black Cat: Claws II",
                year_start=2011,
                comicvine_id=44066,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add(series)
            await session.flush()
            imported = Issue(
                series_id=series.id,
                issue_number=2.0,
                comicvine_id=302780,
                title="Imported file",
                status=IssueStatus.OWNED,
                metadata_source="import",
            )
            session.add(imported)
            await session.flush()

            await service.upsert_issue_summaries(
                session,
                series,
                [
                    IssueSummary(
                        provider_id="302780",
                        issue_number=1.0,
                        title="HC/TPB",
                        release_date="2011-11-30",
                        cover_url="https://example.com/cover.jpg",
                        issue_type="issue",
                    )
                ],
            )

            issues = (
                (await session.execute(select(Issue).where(Issue.series_id == series.id)))
                .scalars()
                .all()
            )
            assert len(issues) == 1
            assert imported.issue_number == 1.0
            assert imported.comicvine_id == 302780
            assert imported.title == "HC/TPB"
            assert imported.status == IssueStatus.OWNED
            assert imported.metadata_source == "comicvine"

    @pytest.mark.asyncio
    async def test_upsert_issue_summaries_skips_provider_id_used_by_other_series(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            other_series = Series(
                title="Other Series",
                sort_title="Other Series",
                year_start=2011,
                comicvine_id=44066,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            target_series = Series(
                title="Target Series",
                sort_title="Target Series",
                year_start=2011,
                comicvine_id=44067,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
            )
            session.add_all([other_series, target_series])
            await session.flush()
            session.add(
                Issue(
                    series_id=other_series.id,
                    issue_number=1.0,
                    comicvine_id=302780,
                    title="Existing provider owner",
                    status=IssueStatus.SKIPPED,
                    metadata_source="comicvine",
                )
            )
            await session.flush()

            await service.upsert_issue_summaries(
                session,
                target_series,
                [
                    IssueSummary(
                        provider_id="302780",
                        issue_number=1.0,
                        title="HC/TPB",
                        release_date="2011-11-30",
                        cover_url=None,
                        issue_type="issue",
                    )
                ],
            )

            target_issue = (
                await session.execute(select(Issue).where(Issue.series_id == target_series.id))
            ).scalar_one()
            assert target_issue.issue_number == 1.0
            assert target_issue.comicvine_id is None
            assert target_issue.title == "HC/TPB"
            assert target_issue.metadata_source == "comicvine"


class TestFetchIssue:
    """Tests for full issue metadata enrichment."""

    @pytest.mark.asyncio
    async def test_fetch_issue_persists_full_metadata_and_creator_credits(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        provider = MagicMock()
        provider.get_issue = AsyncMock(
            return_value=IssueMetadata(
                provider_id="1116296",
                series_provider_id="165083",
                issue_number=1.0,
                title=None,
                description="THE BALANCE OF POWER IS FOREVER CHANGED!",
                release_date="2025-08-01",
                store_date="2025-06-18",
                cover_url="https://example.com/cover.jpg",
                page_count=None,
                comicvine_url=(
                    "https://comicvine.gamespot.com/bring-on-the-bad-guys-doom-1/4000-1116296/"
                ),
                issue_number_text="1au",
                creators=[
                    {"provider_id": "42339", "name": "Marc Guggenheim", "role": "writer"},
                    {"provider_id": "61433", "name": "Neeraj Menon", "role": "colorist"},
                    {"provider_id": "41795", "name": "Travis Lanham", "role": "letterer"},
                ],
            )
        )
        service = MetadataService(provider=provider, covers_dir=MagicMock())

        async with db_factory() as session:
            series = Series(
                title="Bring On the Bad Guys: Doom",
                sort_title="bring on the bad guys doom",
                year_start=2025,
                comicvine_id=165083,
                status=SeriesStatus.ENDED,
                series_type=SeriesType.ONE_SHOT,
            )
            session.add(series)
            await session.flush()
            issue = Issue(
                series_id=series.id,
                comicvine_id=1116296,
                issue_number=1.0,
                title="Stale Title",
                description=None,
                comicvine_url=None,
                status=IssueStatus.OWNED,
            )
            session.add(issue)
            await session.flush()

            enriched = await service.fetch_issue(session, 1116296)

            assert enriched.id == issue.id
            assert enriched.issue_number == 1.0
            assert enriched.issue_number_text == "1AU"
            assert enriched.title is None
            assert enriched.description == "THE BALANCE OF POWER IS FOREVER CHANGED!"
            assert enriched.release_date == date(2025, 8, 1)
            assert enriched.store_date == date(2025, 6, 18)
            assert enriched.cover_url == "https://example.com/cover.jpg"
            assert enriched.comicvine_url.endswith("/4000-1116296/")
            assert enriched.metadata_source == "comicvine"

            rows = (
                await session.execute(
                    select(Creator.name, Creator.comicvine_id, IssueCreator.role)
                    .join(IssueCreator, IssueCreator.creator_id == Creator.id)
                    .where(IssueCreator.issue_id == issue.id)
                    .order_by(Creator.name.asc())
                )
            ).all()
            assert rows == [
                ("Marc Guggenheim", 42339, "writer"),
                ("Neeraj Menon", 61433, "colorist"),
                ("Travis Lanham", 41795, "letterer"),
            ]
