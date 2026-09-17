"""Per-job provider caching for Step 2 import scan workflows.

The scan/review pipeline can ask ComicVine for the same search results,
series metadata, or issue summaries many times while evaluating related
import buckets. A lightweight in-memory wrapper keeps those calls scoped to
one job run without introducing cross-job cache invalidation complexity.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from inspect import iscoroutine
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pullbox.core.issue_numbers import issue_number_lookup_value, normalize_issue_number_queries
from pullbox.core.name_matcher import NameMatcher
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

_T = TypeVar("_T")


def is_in_memory_sqlite_engine(engine: AsyncEngine) -> bool:
    """Return True when separate sessions would share one SQLite memory connection."""
    url = engine.url
    if url.get_backend_name() != "sqlite":
        return False
    database = url.database
    if database in {None, "", ":memory:"}:
        return True
    return str(database).startswith("file:") and "mode=memory" in str(url.query)


def build_import_scan_metadata_provider(
    session: AsyncSession,
    provider: Any,
) -> CachedImportMetadataProvider:
    """Return the Step 2 provider stack: persistent cache, then per-job cache."""
    from pullbox.services.catalog.lookup import catalog_or_provider

    return CachedImportMetadataProvider(
        catalog_or_provider(build_persistent_import_metadata_provider(session, provider))
    )


def build_persistent_import_metadata_provider(
    session: AsyncSession,
    provider: Any,
) -> Any:
    """Return a provider backed by the cross-job ComicVine cache when available."""
    if isinstance(provider, PersistentComicVineCacheProvider):
        return provider

    bind = getattr(session, "bind", None)
    if isinstance(bind, AsyncEngine) and not is_in_memory_sqlite_engine(bind):
        return PersistentComicVineCacheProvider(
            provider,
            async_sessionmaker(bind, expire_on_commit=False),
        )
    return provider


class CachedImportMetadataProvider:
    """Memoize hot provider calls during a single import scan job."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._search_cache: dict[tuple[str, int | None, int, int, bool], asyncio.Task[Any]] = {}
        self._global_search_cache: dict[tuple[str, int, int, bool], asyncio.Task[Any]] = {}
        self._series_cache: dict[str, asyncio.Task[Any]] = {}
        self._issue_cache: dict[str, asyncio.Task[Any]] = {}
        self._issues_cache: dict[str, asyncio.Task[Any]] = {}
        self._issues_by_number_cache: dict[
            tuple[str, tuple[float | str, ...]], asyncio.Task[Any]
        ] = {}
        self._stats = _MemoryCacheStats()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def _memoize(
        self,
        cache: dict[Any, asyncio.Task[_T]],
        key: Any,
        kind: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
    ) -> _T:
        task = cache.get(key)
        if task is None:
            self._stats.misses[kind] += 1
            task = asyncio.create_task(factory())
            cache[key] = task
        else:
            self._stats.hits[kind] += 1
        return await task

    def cache_metrics(self) -> dict[str, Any]:
        """Return per-job in-memory cache metrics plus wrapped-provider metrics."""
        metrics: dict[str, Any] = {
            "memory_hits": _counter_dict(self._stats.hits),
            "memory_misses": _counter_dict(self._stats.misses),
        }
        wrapped_metrics = getattr(self._provider, "cache_metrics", None)
        if callable(wrapped_metrics):
            wrapped_result = wrapped_metrics()
            if iscoroutine(wrapped_result):
                with suppress(Exception):
                    wrapped_result.close()
            elif isinstance(wrapped_result, dict):
                metrics["persistent"] = wrapped_result
        return metrics

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> Any:
        normalized_query = NameMatcher.normalize(query)
        key = (normalized_query, year, limit, offset, suppress_errors)

        async def fetch() -> Any:
            try:
                return await self._provider.search_series(
                    query,
                    year,
                    limit=limit,
                    offset=offset,
                    suppress_errors=suppress_errors,
                )
            except TypeError:
                return await self._provider.search_series(
                    query,
                    year,
                    limit=limit,
                    offset=offset,
                )

        return await self._memoize(
            self._search_cache,
            key,
            "search_series",
            fetch,
        )

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> Any:
        normalized_query = NameMatcher.normalize(query)
        key = (normalized_query, int(max_results), int(batch_size), bool(suppress_errors))

        async def fetch() -> Any:
            global_search = _declared_provider_method(self._provider, "search_series_globally")
            if callable(global_search):
                try:
                    return await global_search(
                        query,
                        max_results=max_results,
                        batch_size=batch_size,
                        suppress_errors=suppress_errors,
                    )
                except TypeError:
                    try:
                        return await global_search(query, max_results=max_results)
                    except TypeError:
                        return await global_search(query)
            results = await self.search_series(
                query,
                None,
                limit=max_results,
                suppress_errors=suppress_errors,
            )
            return results, len(results)

        return await self._memoize(
            self._global_search_cache,
            key,
            "search_series_globally",
            fetch,
        )

    async def get_series(self, series_provider_id: str) -> Any:
        key = str(series_provider_id)
        return await self._memoize(
            self._series_cache,
            key,
            "get_series",
            lambda: self._provider.get_series(series_provider_id),
        )

    async def get_series_cached(self, series_provider_id: str) -> Any | None:
        """Return a memoized/cached series payload without starting a provider fetch."""
        key = str(series_provider_id)
        task = self._series_cache.get(key)
        if task is not None:
            self._stats.hits["get_series"] += 1
            return await task

        cached_lookup = _declared_provider_method(self._provider, "get_series_cached")
        if not callable(cached_lookup):
            return None
        cached_value = await cached_lookup(series_provider_id)
        if cached_value is None:
            return None

        async def fetch_cached() -> Any:
            return cached_value

        self._series_cache[key] = asyncio.create_task(fetch_cached())
        self._stats.hits["get_series"] += 1
        return cached_value

    async def get_issue(self, issue_provider_id: str) -> Any:
        key = str(issue_provider_id)
        return await self._memoize(
            self._issue_cache,
            key,
            "get_issue",
            lambda: self._provider.get_issue(issue_provider_id),
        )

    async def get_issues_for_series(self, series_provider_id: str) -> Any:
        key = str(series_provider_id)
        return await self._memoize(
            self._issues_cache,
            key,
            "get_issues_for_series",
            lambda: self._provider.get_issues_for_series(series_provider_id),
        )

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: Sequence[float | str],
    ) -> Any:
        normalized_numbers = tuple(normalize_issue_number_queries(issue_numbers))
        key = (str(series_provider_id), normalized_numbers)

        async def fetch() -> Any:
            targeted_fetch = _declared_provider_method(
                self._provider,
                "get_issues_for_series_by_numbers",
            )
            if callable(targeted_fetch):
                return await targeted_fetch(
                    series_provider_id,
                    list(normalized_numbers),
                )
            summaries = await self.get_issues_for_series(series_provider_id)
            number_set = set(normalized_numbers)
            return [
                summary
                for summary in summaries
                if issue_number_lookup_value(
                    getattr(summary, "issue_number_text", None) or summary.issue_number
                )
                in number_set
            ]

        return await self._memoize(
            self._issues_by_number_cache,
            key,
            "get_issues_for_series_by_numbers",
            fetch,
        )


@dataclass(slots=True)
class _MemoryCacheStats:
    hits: Counter[str] = field(default_factory=Counter)
    misses: Counter[str] = field(default_factory=Counter)


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def _declared_provider_method(provider: object, name: str) -> Any:
    """Return provider methods declared on the wrapped type, not dynamic mock attributes."""
    if getattr(type(provider), name, None) is None:
        return None
    method = getattr(provider, name, None)
    return method if callable(method) else None
