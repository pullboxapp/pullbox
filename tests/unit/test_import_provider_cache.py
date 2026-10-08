"""Unit coverage for per-job import provider caching."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider
from pullbox.services.import_provider_cache import (
    CachedImportMetadataProvider,
    build_import_scan_metadata_provider,
)


class _ProviderDouble:
    def __init__(self) -> None:
        self.search_calls = 0
        self.global_search_calls = 0
        self.series_calls = 0
        self.issue_calls = 0

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[str]:
        self.search_calls += 1
        return [f"{query}:{year}:{limit}:{offset}"]

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> tuple[list[str], int]:
        self.global_search_calls += 1
        return [f"{query}:{max_results}:{batch_size}:{suppress_errors}"], 1

    async def get_issues_for_series(self, series_provider_id: str) -> list[str]:
        self.issue_calls += 1
        return [series_provider_id]

    async def get_series(self, series_provider_id: str) -> str:
        self.series_calls += 1
        return f"series:{series_provider_id}"


async def test_build_import_scan_metadata_provider_skips_persistent_cache_for_memory_sqlite(
    async_engine,
) -> None:
    provider = AsyncMock()
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with session_factory() as session:
        cached_provider = build_import_scan_metadata_provider(session, provider)

    assert cached_provider._provider is provider


async def test_build_import_scan_metadata_provider_wraps_file_sqlite_with_persistent_cache(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pullbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = AsyncMock()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            cached_provider = build_import_scan_metadata_provider(session, provider)
    finally:
        await engine.dispose()

    assert isinstance(cached_provider._provider, PersistentComicVineCacheProvider)


async def test_build_import_scan_metadata_provider_preserves_existing_persistent_cache(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pullbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = AsyncMock()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    persistent_provider = PersistentComicVineCacheProvider(provider, session_factory)

    try:
        async with session_factory() as session:
            cached_provider = build_import_scan_metadata_provider(session, persistent_provider)
    finally:
        await engine.dispose()

    assert cached_provider._provider is persistent_provider


async def test_cached_import_metadata_provider_normalizes_search_queries() -> None:
    provider = _ProviderDouble()
    cached = CachedImportMetadataProvider(provider)

    first = await cached.search_series("Batman: Year One", 1987)
    second = await cached.search_series("batman year one", 1987)

    assert first == second
    assert provider.search_calls == 1
    assert cached.cache_metrics()["memory_hits"] == {"search_series": 1}
    assert cached.cache_metrics()["memory_misses"] == {"search_series": 1}


async def test_cached_import_metadata_provider_normalizes_global_search_queries() -> None:
    provider = _ProviderDouble()
    cached = CachedImportMetadataProvider(provider)

    first = await cached.search_series_globally("The Punisher", max_results=1000)
    second = await cached.search_series_globally("the punisher", max_results=1000)

    assert first == second
    assert provider.global_search_calls == 1
    assert cached.cache_metrics()["memory_hits"] == {"search_series_globally": 1}
    assert cached.cache_metrics()["memory_misses"] == {"search_series_globally": 1}


async def test_cached_import_metadata_provider_ignores_dynamic_mock_global_search() -> None:
    provider = AsyncMock()
    provider.search_series.return_value = ["Batman"]
    cached = CachedImportMetadataProvider(provider)

    results, total = await cached.search_series_globally("Batman", max_results=1000)

    assert results == ["Batman"]
    assert total == 1
    provider.search_series.assert_awaited_once()
    provider.search_series_globally.assert_not_awaited()


async def test_cached_import_metadata_provider_ignores_dynamic_mock_targeted_issue_lookup() -> None:
    class _Issue:
        def __init__(self, issue_number: float, provider_id: str) -> None:
            self.issue_number = issue_number
            self.provider_id = provider_id

    provider = AsyncMock()
    provider.get_issues_for_series.return_value = [_Issue(1.0, "1"), _Issue(2.0, "2")]
    cached = CachedImportMetadataProvider(provider)

    summaries = await cached.get_issues_for_series_by_numbers("123", [2.0])

    assert [summary.provider_id for summary in summaries] == ["2"]
    provider.get_issues_for_series.assert_awaited_once()
    provider.get_issues_for_series_by_numbers.assert_not_awaited()


async def test_cached_import_metadata_provider_reuses_issue_summaries() -> None:
    provider = _ProviderDouble()
    cached = CachedImportMetadataProvider(provider)

    first = await cached.get_issues_for_series("1234")
    second = await cached.get_issues_for_series("1234")

    assert first == second
    assert provider.issue_calls == 1
    assert cached.cache_metrics()["memory_hits"] == {"get_issues_for_series": 1}
    assert cached.cache_metrics()["memory_misses"] == {"get_issues_for_series": 1}


async def test_cached_import_metadata_provider_cached_only_series_lookup_does_not_fetch() -> None:
    provider = _ProviderDouble()
    cached = CachedImportMetadataProvider(provider)

    assert await cached.get_series_cached("1234") is None
    assert provider.series_calls == 0

    fetched = await cached.get_series("1234")
    cached_only = await cached.get_series_cached("1234")

    assert fetched == "series:1234"
    assert cached_only == "series:1234"
    assert provider.series_calls == 1


async def test_cached_only_issue_batch_never_falls_back_to_remote_fetch() -> None:
    provider = AsyncMock()
    cached = CachedImportMetadataProvider(provider)
    assert await cached.get_issue_batch_cached(["100"]) == {}
    provider.get_issue.assert_not_awaited()
    provider.get_issue_batch.assert_not_awaited()
    provider.get_issue_batch_cached.assert_not_awaited()


async def test_cached_only_issue_batch_reuses_completed_memory_entries() -> None:
    provider = AsyncMock()
    provider.get_issue.return_value = "known issue"
    cached = CachedImportMetadataProvider(provider)
    await cached.get_issue("100")
    assert await cached.get_issue_batch_cached(["100", "101"]) == {"100": "known issue"}
    provider.get_issue.assert_awaited_once_with("100")
