"""Unit coverage for persistent ComicVine import cache."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pullbox.models.provider_cache import MetadataProviderCacheEntry
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata, SeriesSearchResult
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider


class _ComicVineProviderDouble:
    name = "comicvine"

    def __init__(self) -> None:
        self.search_calls = 0
        self.global_search_calls = 0
        self.series_calls = 0
        self.series_batch_calls: list[list[str]] = []
        self.issue_batch_calls: list[list[str]] = []
        self.catalog_batch_calls: list[list[str]] = []
        self.issue_number_calls = 0

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> list[SeriesSearchResult]:
        self.search_calls += 1
        return [
            SeriesSearchResult(
                provider_id="123",
                title=query,
                year_start=year,
                publisher="DC",
                issue_count=12,
                status=None,
                cover_url=None,
                description=None,
            )
        ]

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        self.global_search_calls += 1
        results = [
            SeriesSearchResult(
                provider_id="456",
                title=query,
                year_start=2026,
                publisher="Marvel",
                issue_count=5,
                status=None,
                cover_url=None,
                description=None,
            )
        ]
        return results[:max_results], len(results)

    async def get_series(self, series_provider_id: str) -> SeriesMetadata:
        self.series_calls += 1
        return SeriesMetadata(
            provider_id=str(series_provider_id),
            title="Absolute Cache",
            sort_title="Absolute Cache",
            year_start=2026,
            year_end=None,
            status=None,
            publisher="Pullbox",
            description=None,
            cover_url=None,
            issue_count=4,
            comicvine_url=None,
        )

    async def get_series_batch(
        self,
        series_provider_ids: list[str],
    ) -> dict[str, SeriesMetadata]:
        self.series_batch_calls.append(list(series_provider_ids))
        return {
            provider_id: SeriesMetadata(
                provider_id=provider_id,
                title=f"Series {provider_id}",
                sort_title=f"Series {provider_id}",
                year_start=2026,
                year_end=None,
                status=None,
                publisher="Pullbox",
                description=None,
                cover_url=None,
                issue_count=1,
                comicvine_url=None,
            )
            for provider_id in series_provider_ids
        }

    async def get_issue_batch(
        self,
        issue_provider_ids: list[str],
    ) -> dict[str, IssueMetadata]:
        self.issue_batch_calls.append(list(issue_provider_ids))
        return {
            provider_id: IssueMetadata(
                provider_id=provider_id,
                series_provider_id="123",
                issue_number=float(index),
                title=f"Issue {index}",
                description="Complete issue metadata",
                release_date="2026-01-01",
                store_date="2025-12-31",
                cover_url=None,
                page_count=None,
                comicvine_url=f"https://example.test/issue/{provider_id}",
                creators=[{"name": "Writer", "role": "writer"}],
            )
            for index, provider_id in enumerate(issue_provider_ids, start=1)
        }

    async def get_issue_catalog_batch(
        self,
        series_provider_ids: list[str],
    ) -> dict[str, list[IssueSummary]]:
        self.catalog_batch_calls.append(list(series_provider_ids))
        return {
            provider_id: [
                IssueSummary(
                    provider_id=f"{provider_id}01",
                    issue_number=1.0,
                    title="One",
                    release_date="2026-01-01",
                    cover_url=None,
                    issue_type="issue",
                )
            ]
            for provider_id in series_provider_ids
        }

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: list[float],
    ) -> list[IssueSummary]:
        self.issue_number_calls += 1
        return [
            IssueSummary(
                provider_id=f"{series_provider_id}-{int(number)}",
                issue_number=float(number),
                title=f"Issue {int(number)}",
                release_date="2026-01-01",
                cover_url=None,
                issue_type="issue",
            )
            for number in issue_numbers
        ]


class _EmptyTargetedIssueProvider:
    name = "comicvine"

    def __init__(self) -> None:
        self.issue_number_calls = 0

    async def get_issues_for_series_by_numbers(
        self,
        _series_provider_id: str,
        _issue_numbers: list[float],
    ) -> list[IssueSummary]:
        self.issue_number_calls += 1
        return []


class _SlowGlobalSearchProvider(_ComicVineProviderDouble):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        self.global_search_calls += 1
        self.started.set()
        await self.release.wait()
        results = [
            SeriesSearchResult(
                provider_id="999",
                title=query,
                year_start=2026,
                publisher="DC",
                issue_count=1,
                status=None,
                cover_url=None,
                description=None,
            )
        ]
        return results[:max_results], len(results)


async def test_persistent_cache_reuses_search_results_across_wrappers(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()

    first_cache = PersistentComicVineCacheProvider(provider, session_factory)
    first_result = await first_cache.search_series("The Punisher", 2026, limit=10)

    second_cache = PersistentComicVineCacheProvider(provider, session_factory)
    second_result = await second_cache.search_series("the punisher", 2026, limit=10)

    assert first_result == second_result
    assert provider.search_calls == 1
    assert second_cache.cache_metrics()["hits"] == {"search_series": 1}


async def test_persistent_cache_reuses_global_search_results_across_wrappers(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()

    first_cache = PersistentComicVineCacheProvider(provider, session_factory)
    first_result = await first_cache.search_series_globally("The Punisher", max_results=1000)

    second_cache = PersistentComicVineCacheProvider(provider, session_factory)
    second_result = await second_cache.search_series_globally("the punisher", max_results=1000)

    assert first_result == second_result
    assert provider.global_search_calls == 1
    assert second_cache.cache_metrics()["hits"] == {"search_series_globally": 1}


async def test_persistent_cache_collapses_concurrent_global_search_misses(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _SlowGlobalSearchProvider()

    first_cache = PersistentComicVineCacheProvider(provider, session_factory)
    second_cache = PersistentComicVineCacheProvider(provider, session_factory)

    first_task = asyncio.create_task(first_cache.search_series_globally("Batman", max_results=1000))
    await provider.started.wait()
    second_task = asyncio.create_task(
        second_cache.search_series_globally("batman", max_results=1000)
    )
    await asyncio.sleep(0)

    provider.release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result == second_result
    assert provider.global_search_calls == 1
    assert first_cache.cache_metrics()["stores"] == {"search_series_globally": 1}
    assert second_cache.cache_metrics()["stores"] == {}


async def test_persistent_cache_ignores_dynamic_mock_global_search(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = AsyncMock()
    provider.search_series.return_value = [
        SeriesSearchResult(
            provider_id="789",
            title="Batman",
            year_start=2016,
            publisher="DC",
            issue_count=12,
            status=None,
            cover_url=None,
            description=None,
        )
    ]

    cache = PersistentComicVineCacheProvider(provider, session_factory)
    results, total = await cache.search_series_globally("Batman", max_results=1000)

    assert [result.provider_id for result in results] == ["789"]
    assert total == 1
    provider.search_series.assert_awaited_once()
    provider.search_series_globally.assert_not_awaited()


async def test_persistent_cache_ignores_dynamic_mock_targeted_issue_lookup(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = AsyncMock()
    provider.get_issues_for_series.return_value = [
        IssueSummary(
            provider_id="1",
            issue_number=1.0,
            title="One",
            release_date="2026-01-01",
            cover_url=None,
            issue_type="issue",
        ),
        IssueSummary(
            provider_id="2",
            issue_number=2.0,
            title="Two",
            release_date="2026-02-01",
            cover_url=None,
            issue_type="issue",
        ),
    ]

    cache = PersistentComicVineCacheProvider(provider, session_factory)
    summaries = await cache.get_issues_for_series_by_numbers("123", [2.0])

    assert [summary.provider_id for summary in summaries] == ["2"]
    provider.get_issues_for_series.assert_awaited_once()
    provider.get_issues_for_series_by_numbers.assert_not_awaited()


async def test_persistent_cache_reuses_series_detail_payload(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()

    first_cache = PersistentComicVineCacheProvider(provider, session_factory)
    first_result = await first_cache.get_series("123")

    second_cache = PersistentComicVineCacheProvider(provider, session_factory)
    second_result = await second_cache.get_series("123")

    assert first_result == second_result
    assert provider.series_calls == 1


async def test_persistent_cache_cached_only_series_lookup_does_not_fetch(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()

    cache = PersistentComicVineCacheProvider(provider, session_factory)

    assert await cache.get_series_cached("123") is None
    assert provider.series_calls == 0

    fetched = await cache.get_series("123")
    cached_only = await cache.get_series_cached("123")

    assert cached_only == fetched
    assert provider.series_calls == 1
    assert cache.cache_metrics()["hits"] == {"get_series": 1}


async def test_persistent_cache_bulk_fetches_only_missing_series_and_reuses_rows(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()
    cache = PersistentComicVineCacheProvider(provider, session_factory)
    await cache.get_series("1")

    first = await cache.get_series_batch(["1", "2", "3"])
    second = await PersistentComicVineCacheProvider(provider, session_factory).get_series_batch(
        ["3", "2", "1"]
    )

    assert list(first) == ["1", "2", "3"]
    assert list(second) == ["3", "2", "1"]
    assert provider.series_batch_calls == [["2", "3"]]


async def test_persistent_cache_bulk_catalog_and_issue_metadata_populate_singular_keys(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()
    cache = PersistentComicVineCacheProvider(provider, session_factory)

    catalogs = await cache.get_issue_catalog_batch(["10", "20"])
    issues = await cache.get_issue_batch(["101", "201"])
    cached_catalog = await PersistentComicVineCacheProvider(
        provider, session_factory
    ).get_issues_for_series("10")
    cached_issue = await PersistentComicVineCacheProvider(provider, session_factory).get_issue(
        "101"
    )

    assert catalogs["10"][0].provider_id == "1001"
    assert issues["101"].description == "Complete issue metadata"
    assert cached_catalog == catalogs["10"]
    assert cached_issue == issues["101"]
    assert provider.catalog_batch_calls == [["10", "20"]]
    assert provider.issue_batch_calls == [["101", "201"]]


async def test_persistent_cache_forced_refresh_replaces_singular_series_and_catalog_rows(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()
    cache = PersistentComicVineCacheProvider(provider, session_factory)
    await cache.get_series("77")
    await cache.get_issue_catalog_batch(["77"])

    refreshed_series = await cache.refresh_series("77")
    refreshed_catalog = await cache.refresh_issue_catalog("77")

    assert refreshed_series.provider_id == "77"
    assert refreshed_catalog[0].provider_id == "7701"
    assert provider.series_calls == 2
    assert provider.catalog_batch_calls == [["77"], ["77"]]


async def test_persistent_cache_stores_targeted_issue_numbers_individually(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()

    first_cache = PersistentComicVineCacheProvider(provider, session_factory)
    first_result = await first_cache.get_issues_for_series_by_numbers("555", [1, 2])

    second_cache = PersistentComicVineCacheProvider(provider, session_factory)
    second_result = await second_cache.get_issues_for_series_by_numbers("555", [2])

    assert [summary.issue_number for summary in first_result] == [1.0, 2.0]
    assert [summary.issue_number for summary in second_result] == [2.0]
    assert provider.issue_number_calls == 1
    assert second_cache.cache_metrics()["hits"] == {"get_issues_for_series_by_number": 1}


async def test_persistent_cache_empty_targeted_issue_number_uses_short_ttl(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _EmptyTargetedIssueProvider()
    now = datetime(2026, 5, 29, tzinfo=UTC)

    empty_cache = PersistentComicVineCacheProvider(
        provider,
        session_factory,
        now_func=lambda: now,
    )
    await empty_cache.get_issues_for_series_by_numbers("555", [999])

    async with session_factory() as session:
        row = (
            await session.scalars(
                select(MetadataProviderCacheEntry).where(
                    MetadataProviderCacheEntry.cache_kind == "get_issues_for_series_by_number"
                )
            )
        ).one()

    assert row.expires_at == now + timedelta(minutes=15)


async def test_persistent_cache_refreshes_stale_rows(
    async_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    provider = _ComicVineProviderDouble()
    now = datetime(2026, 5, 29, tzinfo=UTC)

    first_cache = PersistentComicVineCacheProvider(
        provider,
        session_factory,
        stale_after=timedelta(minutes=5),
        now_func=lambda: now,
    )
    await first_cache.search_series("Batman", 2026, limit=10)

    stale_cache = PersistentComicVineCacheProvider(
        provider,
        session_factory,
        stale_after=timedelta(minutes=5),
        now_func=lambda: now + timedelta(minutes=6),
    )
    await stale_cache.search_series("Batman", 2026, limit=10)

    assert provider.search_calls == 2
    assert stale_cache.cache_metrics()["misses"] == {"search_series": 1}

    async with session_factory() as session:
        rows = (await session.scalars(select(MetadataProviderCacheEntry))).all()

    assert len(rows) == 1
