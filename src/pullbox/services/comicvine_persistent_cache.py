"""Persistent ComicVine cache wrapper used by import matching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.provider_cache import MetadataProviderCacheEntry
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata, SeriesSearchResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_T = TypeVar("_T")

DEFAULT_COMICVINE_CACHE_TTL = timedelta(hours=24)
EMPTY_TARGETED_ISSUE_CACHE_TTL = timedelta(minutes=15)
_PROVIDER_NAME = "comicvine"
_CACHE_MISS_INFLIGHT: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ComicVinePersistentCacheStats:
    """In-memory counters for one import run's persistent-cache behavior."""

    hits: Counter[str] = field(default_factory=Counter)
    misses: Counter[str] = field(default_factory=Counter)
    stores: Counter[str] = field(default_factory=Counter)
    external_calls: Counter[str] = field(default_factory=Counter)
    external_duration_ms: defaultdict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly metrics snapshot."""
        return {
            "hits": _counter_dict(self.hits),
            "misses": _counter_dict(self.misses),
            "stores": _counter_dict(self.stores),
            "external_calls": _counter_dict(self.external_calls),
            "external_duration_ms": {
                key: round(value, 2) for key, value in sorted(self.external_duration_ms.items())
            },
        }


class PersistentComicVineCacheProvider:
    """Cache ComicVine metadata responses in the app database.

    The wrapper intentionally leaves matching decisions untouched. It only
    returns previously fetched provider DTOs while they are fresh.
    """

    def __init__(
        self,
        provider: Any,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        stale_after: timedelta = DEFAULT_COMICVINE_CACHE_TTL,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._stale_after = stale_after
        self._now_func = now_func or _utc_now
        self._stats = ComicVinePersistentCacheStats()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    @property
    def name(self) -> str:
        return str(getattr(self._provider, "name", _PROVIDER_NAME))

    def cache_metrics(self) -> dict[str, Any]:
        """Return current cache metrics for import timing logs."""
        return self._stats.as_dict()

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> list[SeriesSearchResult]:
        request = {
            "query": NameMatcher.normalize(query),
            "year": year,
            "limit": int(limit),
            "offset": int(offset),
            "suppress_errors": bool(suppress_errors),
        }

        async def fetch() -> list[SeriesSearchResult]:
            return await self._search_series_uncached(
                query,
                year,
                limit=limit,
                offset=offset,
                suppress_errors=suppress_errors,
            )

        return await self._get_or_fetch(
            "search_series",
            request,
            fetch,
            _series_search_results_to_payload,
            _series_search_results_from_payload,
        )

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        request = {
            "query": NameMatcher.normalize(query),
            "max_results": int(max_results),
            "batch_size": int(batch_size),
            "suppress_errors": bool(suppress_errors),
        }

        async def fetch() -> tuple[list[SeriesSearchResult], int]:
            return await self._search_series_globally_uncached(
                query,
                max_results=max_results,
                batch_size=batch_size,
                suppress_errors=suppress_errors,
            )

        return await self._get_or_fetch(
            "search_series_globally",
            request,
            fetch,
            _global_series_search_results_to_payload,
            _global_series_search_results_from_payload,
        )

    async def get_series(self, series_provider_id: str) -> SeriesMetadata:
        request = {"series_provider_id": str(series_provider_id)}
        return await self._get_or_fetch(
            "get_series",
            request,
            lambda: self._provider.get_series(series_provider_id),
            _series_metadata_to_payload,
            _series_metadata_from_payload,
        )

    async def get_series_batch(
        self,
        series_provider_ids: list[str],
    ) -> dict[str, SeriesMetadata]:
        """Resolve series profiles from singular cache rows, batching misses upstream."""
        provider_ids = _ordered_provider_ids(series_provider_ids)
        found: dict[str, SeriesMetadata] = {}
        missing: list[str] = []
        for provider_id in provider_ids:
            request = {"series_provider_id": provider_id}
            payload = await self._load_cached_payload("get_series", _cache_key(request))
            if payload is None:
                self._stats.misses["get_series"] += 1
                missing.append(provider_id)
            else:
                self._stats.hits["get_series"] += 1
                found[provider_id] = _series_metadata_from_payload(payload)

        if missing:
            started_at = time.monotonic()
            batch_fetch = _declared_provider_method(self._provider, "get_series_batch")
            if callable(batch_fetch):
                fetched = await batch_fetch(missing)
            else:
                fetched = {
                    provider_id: await self._provider.get_series(provider_id)
                    for provider_id in missing
                }
            self._stats.external_calls["get_series"] += 1
            self._stats.external_duration_ms["get_series"] += (time.monotonic() - started_at) * 1000
            for provider_id in missing:
                metadata = fetched.get(provider_id)
                if metadata is None:
                    continue
                found[provider_id] = metadata
                request = {"series_provider_id": provider_id}
                await self._store_payload(
                    "get_series",
                    _cache_key(request),
                    request,
                    _series_metadata_to_payload(metadata),
                )
        return {
            provider_id: found[provider_id] for provider_id in provider_ids if provider_id in found
        }

    async def get_series_cached(self, series_provider_id: str) -> SeriesMetadata | None:
        """Return cached series metadata without making a provider request."""
        request = {"series_provider_id": str(series_provider_id)}
        payload = await self._load_cached_payload("get_series", _cache_key(request))
        if payload is None:
            return None
        self._stats.hits["get_series"] += 1
        return _series_metadata_from_payload(payload)

    async def refresh_series(self, series_provider_id: str) -> SeriesMetadata:
        """Bypass a fresh cache row, then replace it with current provider data."""
        metadata = cast("SeriesMetadata", await self._provider.get_series(series_provider_id))
        request = {"series_provider_id": str(series_provider_id)}
        await self._store_payload(
            "get_series",
            _cache_key(request),
            request,
            _series_metadata_to_payload(metadata),
        )
        return metadata

    async def get_issue(self, issue_provider_id: str) -> IssueMetadata:
        request = {"issue_provider_id": str(issue_provider_id)}
        return await self._get_or_fetch(
            "get_issue",
            request,
            lambda: self._provider.get_issue(issue_provider_id),
            _issue_metadata_to_payload,
            _issue_metadata_from_payload,
        )

    async def get_issue_batch(
        self,
        issue_provider_ids: list[str],
    ) -> dict[str, IssueMetadata]:
        """Resolve full issue metadata from singular cache rows and one batch miss path."""
        provider_ids = _ordered_provider_ids(issue_provider_ids)
        found: dict[str, IssueMetadata] = {}
        missing: list[str] = []
        for provider_id in provider_ids:
            request = {"issue_provider_id": provider_id}
            payload = await self._load_cached_payload("get_issue", _cache_key(request))
            if payload is None:
                self._stats.misses["get_issue"] += 1
                missing.append(provider_id)
            else:
                self._stats.hits["get_issue"] += 1
                found[provider_id] = _issue_metadata_from_payload(payload)

        if missing:
            started_at = time.monotonic()
            batch_fetch = _declared_provider_method(self._provider, "get_issue_batch")
            if callable(batch_fetch):
                fetched = await batch_fetch(missing)
            else:
                fetched = {
                    provider_id: await self._provider.get_issue(provider_id)
                    for provider_id in missing
                }
            self._stats.external_calls["get_issue"] += 1
            self._stats.external_duration_ms["get_issue"] += (time.monotonic() - started_at) * 1000
            for provider_id in missing:
                metadata = fetched.get(provider_id)
                if metadata is None:
                    continue
                found[provider_id] = metadata
                request = {"issue_provider_id": provider_id}
                await self._store_payload(
                    "get_issue",
                    _cache_key(request),
                    request,
                    _issue_metadata_to_payload(metadata),
                )
        return {
            provider_id: found[provider_id] for provider_id in provider_ids if provider_id in found
        }

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        request = {"series_provider_id": str(series_provider_id)}
        return await self._get_or_fetch(
            "get_issues_for_series",
            request,
            lambda: self._provider.get_issues_for_series(series_provider_id),
            _issue_summaries_to_payload,
            _issue_summaries_from_payload,
        )

    async def get_issue_catalog_batch(
        self,
        series_provider_ids: list[str],
    ) -> dict[str, list[IssueSummary]]:
        """Resolve issue catalogs from singular cache rows, batching missing volumes."""
        provider_ids = _ordered_provider_ids(series_provider_ids)
        found: dict[str, list[IssueSummary]] = {}
        missing: list[str] = []
        for provider_id in provider_ids:
            request = {"series_provider_id": provider_id}
            payload = await self._load_cached_payload(
                "get_issues_for_series",
                _cache_key(request),
            )
            if payload is None:
                self._stats.misses["get_issues_for_series"] += 1
                missing.append(provider_id)
            else:
                self._stats.hits["get_issues_for_series"] += 1
                found[provider_id] = _issue_summaries_from_payload(payload)

        if missing:
            started_at = time.monotonic()
            batch_fetch = _declared_provider_method(self._provider, "get_issue_catalog_batch")
            if callable(batch_fetch):
                fetched = await batch_fetch(missing)
            else:
                fetched = {
                    provider_id: await self._provider.get_issues_for_series(provider_id)
                    for provider_id in missing
                }
            self._stats.external_calls["get_issues_for_series"] += 1
            self._stats.external_duration_ms["get_issues_for_series"] += (
                time.monotonic() - started_at
            ) * 1000
            for provider_id in missing:
                summaries = fetched.get(provider_id)
                if summaries is None:
                    continue
                found[provider_id] = summaries
                request = {"series_provider_id": provider_id}
                await self._store_payload(
                    "get_issues_for_series",
                    _cache_key(request),
                    request,
                    _issue_summaries_to_payload(summaries),
                )
        return {
            provider_id: found[provider_id] for provider_id in provider_ids if provider_id in found
        }

    async def refresh_issue_catalog(self, series_provider_id: str) -> list[IssueSummary]:
        """Bypass a fresh catalog cache row, then replace it atomically."""
        batch_fetch = _declared_provider_method(self._provider, "get_issue_catalog_batch")
        if callable(batch_fetch):
            summaries = cast(
                "list[IssueSummary]",
                (await batch_fetch([str(series_provider_id)])).get(
                    str(series_provider_id),
                    [],
                ),
            )
        else:
            summaries = cast(
                "list[IssueSummary]",
                await self._provider.get_issues_for_series(series_provider_id),
            )
        request = {"series_provider_id": str(series_provider_id)}
        await self._store_payload(
            "get_issues_for_series",
            _cache_key(request),
            request,
            _issue_summaries_to_payload(summaries),
        )
        return summaries

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: list[float],
    ) -> list[IssueSummary]:
        kind = "get_issues_for_series_by_number"
        normalized_numbers = sorted({float(number) for number in issue_numbers})
        summaries: list[IssueSummary] = []
        missing_numbers: list[float] = []

        for issue_number in normalized_numbers:
            request = _issue_number_request(series_provider_id, issue_number)
            payload = await self._load_cached_payload(kind, _cache_key(request))
            if payload is None:
                self._stats.misses[kind] += 1
                missing_numbers.append(issue_number)
                continue
            self._stats.hits[kind] += 1
            summaries.extend(_issue_summaries_from_payload(payload))

        if not missing_numbers:
            return summaries

        started_at = time.monotonic()
        targeted_fetch = _declared_provider_method(
            self._provider,
            "get_issues_for_series_by_numbers",
        )
        if callable(targeted_fetch):
            fetched = await targeted_fetch(
                series_provider_id,
                missing_numbers,
            )
        else:
            missing_set = {float(number) for number in missing_numbers}
            all_summaries = await self.get_issues_for_series(series_provider_id)
            fetched = [
                summary for summary in all_summaries if float(summary.issue_number) in missing_set
            ]
        self._stats.external_calls[kind] += len(missing_numbers)
        self._stats.external_duration_ms[kind] += (time.monotonic() - started_at) * 1000

        by_number: dict[float, list[IssueSummary]] = {number: [] for number in missing_numbers}
        for summary in fetched:
            by_number.setdefault(float(summary.issue_number), []).append(summary)

        for issue_number in missing_numbers:
            number_summaries = by_number.get(issue_number, [])
            await self._store_payload(
                kind,
                _cache_key(_issue_number_request(series_provider_id, issue_number)),
                _issue_number_request(series_provider_id, issue_number),
                _issue_summaries_to_payload(number_summaries),
                ttl=(EMPTY_TARGETED_ISSUE_CACHE_TTL if not number_summaries else self._stale_after),
            )
            summaries.extend(number_summaries)

        return summaries

    async def _search_series_uncached(
        self,
        query: str,
        year: int | None,
        *,
        limit: int,
        offset: int,
        suppress_errors: bool,
    ) -> list[SeriesSearchResult]:
        try:
            return cast(
                "list[SeriesSearchResult]",
                await self._provider.search_series(
                    query,
                    year,
                    limit=limit,
                    offset=offset,
                    suppress_errors=suppress_errors,
                ),
            )
        except TypeError:
            try:
                return cast(
                    "list[SeriesSearchResult]",
                    await self._provider.search_series(
                        query,
                        year,
                        limit=limit,
                        offset=offset,
                    ),
                )
            except TypeError:
                return cast(
                    "list[SeriesSearchResult]",
                    await self._provider.search_series(query, year),
                )

    async def _search_series_globally_uncached(
        self,
        query: str,
        *,
        max_results: int,
        batch_size: int,
        suppress_errors: bool,
    ) -> tuple[list[SeriesSearchResult], int]:
        global_search = _declared_provider_method(self._provider, "search_series_globally")
        if callable(global_search):
            try:
                return cast(
                    "tuple[list[SeriesSearchResult], int]",
                    await global_search(
                        query,
                        max_results=max_results,
                        batch_size=batch_size,
                        suppress_errors=suppress_errors,
                    ),
                )
            except TypeError:
                try:
                    return cast(
                        "tuple[list[SeriesSearchResult], int]",
                        await global_search(query, max_results=max_results),
                    )
                except TypeError:
                    return cast(
                        "tuple[list[SeriesSearchResult], int]",
                        await global_search(query),
                    )
        results = await self._search_series_uncached(
            query,
            None,
            limit=max_results,
            offset=0,
            suppress_errors=suppress_errors,
        )
        return results, len(results)

    async def _get_or_fetch(
        self,
        kind: str,
        request: dict[str, Any],
        fetch: Callable[[], Awaitable[_T]],
        encode: Callable[[_T], dict[str, Any]],
        decode: Callable[[dict[str, Any]], _T],
    ) -> _T:
        cache_key = _cache_key(request)
        cached_payload = await self._load_cached_payload(kind, cache_key)
        if cached_payload is not None:
            self._stats.hits[kind] += 1
            return decode(cached_payload)

        self._stats.misses[kind] += 1
        inflight_key = (kind, cache_key)
        task = _CACHE_MISS_INFLIGHT.get(inflight_key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_store_payload(
                    kind,
                    cache_key,
                    request,
                    fetch,
                    encode,
                )
            )
            _CACHE_MISS_INFLIGHT[inflight_key] = task
            task.add_done_callback(lambda completed: _clear_inflight(inflight_key, completed))

        payload = await asyncio.shield(task)
        return decode(payload)

    async def _fetch_store_payload(
        self,
        kind: str,
        cache_key: str,
        request: dict[str, Any],
        fetch: Callable[[], Awaitable[_T]],
        encode: Callable[[_T], dict[str, Any]],
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        value = await fetch()
        self._stats.external_calls[kind] += 1
        self._stats.external_duration_ms[kind] += (time.monotonic() - started_at) * 1000
        payload = encode(value)
        await self._store_payload(kind, cache_key, request, payload)
        return payload

    async def _load_cached_payload(
        self,
        cache_kind: str,
        cache_key: str,
    ) -> dict[str, Any] | None:
        now = self._now()
        try:
            async with self._session_factory() as session:
                row = await session.scalar(
                    select(MetadataProviderCacheEntry).where(
                        MetadataProviderCacheEntry.provider_name == _PROVIDER_NAME,
                        MetadataProviderCacheEntry.cache_kind == cache_kind,
                        MetadataProviderCacheEntry.cache_key == cache_key,
                    )
                )
                if row is None or row.expires_at <= now:
                    return None
                return dict(row.payload or {})
        except SQLAlchemyError as exc:
            logger.debug(
                "comicvine_persistent_cache_read_failed",
                cache_kind=cache_kind,
                error=str(exc),
            )
            return None

    async def _store_payload(
        self,
        cache_kind: str,
        cache_key: str,
        request: dict[str, Any],
        payload: dict[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        now = self._now()
        expires_at = now + (ttl or self._stale_after)
        async with self._session_factory() as session:
            try:
                await self._upsert_payload(
                    session,
                    cache_kind=cache_kind,
                    cache_key=cache_key,
                    request=request,
                    payload=payload,
                    fetched_at=now,
                    expires_at=expires_at,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                try:
                    await self._upsert_payload(
                        session,
                        cache_kind=cache_kind,
                        cache_key=cache_key,
                        request=request,
                        payload=payload,
                        fetched_at=now,
                        expires_at=expires_at,
                    )
                    await session.commit()
                except SQLAlchemyError as exc:
                    await session.rollback()
                    logger.debug(
                        "comicvine_persistent_cache_write_failed",
                        cache_kind=cache_kind,
                        error=str(exc),
                    )
                    return
            except SQLAlchemyError as exc:
                await session.rollback()
                logger.debug(
                    "comicvine_persistent_cache_write_failed",
                    cache_kind=cache_kind,
                    error=str(exc),
                )
                return
        self._stats.stores[cache_kind] += 1

    async def _upsert_payload(
        self,
        session: AsyncSession,
        *,
        cache_kind: str,
        cache_key: str,
        request: dict[str, Any],
        payload: dict[str, Any],
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        row = await session.scalar(
            select(MetadataProviderCacheEntry).where(
                MetadataProviderCacheEntry.provider_name == _PROVIDER_NAME,
                MetadataProviderCacheEntry.cache_kind == cache_kind,
                MetadataProviderCacheEntry.cache_key == cache_key,
            )
        )
        if row is None:
            session.add(
                MetadataProviderCacheEntry(
                    provider_name=_PROVIDER_NAME,
                    cache_kind=cache_kind,
                    cache_key=cache_key,
                    request=request,
                    payload=payload,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
            )
            return

        row.request = request
        row.payload = payload
        row.fetched_at = fetched_at
        row.expires_at = expires_at

    def _now(self) -> datetime:
        now = self._now_func()
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)


def _cache_key(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ordered_provider_ids(provider_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(str(provider_id) for provider_id in provider_ids))


def _clear_inflight(
    inflight_key: tuple[str, str],
    completed: asyncio.Task[dict[str, Any]],
) -> None:
    if _CACHE_MISS_INFLIGHT.get(inflight_key) is completed:
        _CACHE_MISS_INFLIGHT.pop(inflight_key, None)


def _issue_number_request(series_provider_id: str, issue_number: float) -> dict[str, Any]:
    return {
        "series_provider_id": str(series_provider_id),
        "issue_number": float(issue_number),
    }


def _series_search_results_to_payload(results: list[SeriesSearchResult]) -> dict[str, Any]:
    return {"items": [asdict(result) for result in results]}


def _series_search_results_from_payload(payload: dict[str, Any]) -> list[SeriesSearchResult]:
    return [SeriesSearchResult(**item) for item in payload.get("items", [])]


def _global_series_search_results_to_payload(
    results: tuple[list[SeriesSearchResult], int],
) -> dict[str, Any]:
    items, total_results = results
    return {
        "items": [asdict(result) for result in items],
        "total_results": int(total_results),
    }


def _global_series_search_results_from_payload(
    payload: dict[str, Any],
) -> tuple[list[SeriesSearchResult], int]:
    items = [SeriesSearchResult(**item) for item in payload.get("items", [])]
    return items, int(payload.get("total_results") or len(items))


def _declared_provider_method(provider: object, name: str) -> Any:
    """Return provider methods declared on the wrapped type, not dynamic mock attributes."""
    if getattr(type(provider), name, None) is None:
        return None
    method = getattr(provider, name, None)
    return method if callable(method) else None


def _series_metadata_to_payload(metadata: SeriesMetadata) -> dict[str, Any]:
    return {"item": asdict(metadata)}


def _series_metadata_from_payload(payload: dict[str, Any]) -> SeriesMetadata:
    return SeriesMetadata(**payload["item"])


def _issue_metadata_to_payload(metadata: IssueMetadata) -> dict[str, Any]:
    return {"item": asdict(metadata)}


def _issue_metadata_from_payload(payload: dict[str, Any]) -> IssueMetadata:
    return IssueMetadata(**payload["item"])


def _issue_summaries_to_payload(summaries: list[IssueSummary]) -> dict[str, Any]:
    return {"items": [asdict(summary) for summary in summaries]}


def _issue_summaries_from_payload(payload: dict[str, Any]) -> list[IssueSummary]:
    return [IssueSummary(**item) for item in payload.get("items", [])]


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def _utc_now() -> datetime:
    return datetime.now(UTC)
