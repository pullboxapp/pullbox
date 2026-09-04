"""ComicVine metadata provider implementation.

Integrates with the ComicVine API (https://comicvine.gamespot.com/api/) to
retrieve series, issue, and publisher metadata.  Implements the MetadataProvider
protocol with token-bucket rate limiting and structured logging on every
external call.

ComicVine terminology mapping: "volume" → "series" in Pullbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
import weakref
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from pullbox.core.issue_numbers import format_issue_number, parse_issue_number_text
from pullbox.core.naming import detect_issue_type
from pullbox.providers.base import (
    IssueMetadata,
    IssueSummary,
    ProviderHealthResult,
    SeriesMetadata,
    SeriesSearchResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pullbox.providers.story_arcs import StoryArcMetadata, StoryArcSearchResult

logger = structlog.get_logger(__name__)

# ComicVine API status codes
_STATUS_OK = 1
_STATUS_INVALID_KEY = 100
_STATUS_NOT_FOUND = 101
_STATUS_RATE_LIMITED = 107

# ComicVine resource type prefixes used in detail URLs
_VOLUME_PREFIX = "4050"
_ISSUE_PREFIX = "4000"

_BASE_URL = "https://comicvine.gamespot.com/api"
_REQUEST_TIMEOUT = 15.0
_GLOBAL_SERIES_SEARCH_BATCH_SIZE = 100
_GLOBAL_SERIES_SEARCH_MAX_RESULTS = 1000
_GLOBAL_SERIES_SEARCH_CACHE_TTL_SECONDS = 300.0
_BULK_RESOURCE_PAGE_SIZE = 100
_MAX_BULK_PROVIDER_IDS = 5000
_GLOBAL_SERIES_SEARCH_CACHE: dict[
    tuple[str, int, int],
    tuple[float, tuple[SeriesSearchResult, ...], int],
] = {}
_GLOBAL_SERIES_SEARCH_INFLIGHT: dict[
    tuple[str, int, int, bool],
    asyncio.Task[tuple[tuple[SeriesSearchResult, ...], int]],
] = {}
_GlobalSeriesSearchInflightKey = tuple[str, int, int, bool]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Token bucket rate limiter for ComicVine's ~200 requests/hour limit."""

    def __init__(self, max_tokens: int, refill_rate: float) -> None:
        self._max_tokens = float(max(max_tokens, 1))
        self._tokens = float(max(max_tokens, 1))
        self._refill_rate = refill_rate  # tokens per second
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / self._refill_rate
                await asyncio.sleep(wait_time)
                self._refill()
            self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now


class _RequestVelocityGate:
    """Serialize request starts without conflating velocity and hourly quotas."""

    def __init__(self) -> None:
        self._last_started_at: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self, requests_per_second: int) -> None:
        minimum_interval = 1.0 / max(int(requests_per_second), 1)
        async with self._lock:
            now = time.monotonic()
            if self._last_started_at is not None:
                wait_seconds = minimum_interval - (now - self._last_started_at)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            self._last_started_at = time.monotonic()


class _ComicVineRateCoordinator:
    """Share per-resource hourly budgets and velocity pacing across clients."""

    def __init__(self, rate_limit: int) -> None:
        self._rate_limit = max(int(rate_limit), 1)
        self._resource_limiters: dict[str, _TokenBucket] = {}
        self._velocity_gate = _RequestVelocityGate()

    async def acquire(self, resource: str, *, requests_per_second: int) -> None:
        limiter = self._resource_limiters.get(resource)
        if limiter is None:
            limiter = _TokenBucket(
                max_tokens=self._rate_limit,
                refill_rate=self._rate_limit / 3600.0,
            )
            self._resource_limiters[resource] = limiter
        await limiter.acquire()
        await self._velocity_gate.acquire(requests_per_second)


_RATE_COORDINATORS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, int], _ComicVineRateCoordinator],
] = weakref.WeakKeyDictionary()


def _resource_rate_key(endpoint: str) -> str:
    """Return ComicVine's first path segment, which defines its API resource."""
    return endpoint.strip("/").partition("/")[0] or "unknown"


def _rate_coordinator_for(
    *,
    api_key: str,
    rate_limit: int,
    requests_per_second: int,
) -> _ComicVineRateCoordinator:
    """Return one process-local rate coordinator without retaining an API key."""
    _ = requests_per_second
    loop = asyncio.get_running_loop()
    coordinators = _RATE_COORDINATORS.setdefault(loop, {})
    key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    normalized_rate_limit = max(int(rate_limit), 1)
    coordinator_key = (key_fingerprint, normalized_rate_limit)
    coordinator = coordinators.get(coordinator_key)
    if coordinator is None:
        coordinator = _ComicVineRateCoordinator(normalized_rate_limit)
        coordinators[coordinator_key] = coordinator
    return coordinator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_html(text: str | None) -> str | None:
    """Remove HTML tags and decode entities from ComicVine descriptions."""
    if not text:
        return text
    clean = re.sub(r"<[^>]+>", "", text)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def _parse_issue_number(value: str | None) -> float:
    """Parse ComicVine issue number string to float.

    Handles fraction characters (½ → 0.5) and numeric strings.  Falls back
    to 0.0 for unparseable values like "Annual 1".
    """
    return _parse_issue_number_fields(value)[0]


def _parse_issue_number_fields(value: str | None) -> tuple[float, str | None]:
    """Parse ComicVine numeric compatibility and preserve exact raw semantics."""
    if value is None:
        return 0.0, None
    try:
        return parse_issue_number_text(value)
    except ValueError:
        return 0.0, None


def _format_issue_number_filter(value: float) -> str:
    """Format an issue number for ComicVine's exact issue_number filter."""
    return format_issue_number(value)


def _safe_int(value: Any) -> int | None:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _comicvine_name_filter(query: str) -> str:
    """Build a Mylar-style ComicVine volume-name filter from a free-text query."""
    terms = [term for term in re.findall(r"\w+", query, flags=re.UNICODE) if term]
    return ",".join(f"name:{term}" for term in terms)


def _series_search_result_from_item(item: dict[str, Any]) -> SeriesSearchResult:
    item_year = _safe_int(item.get("start_year"))

    publisher_data = item.get("publisher")
    publisher_name = publisher_data.get("name") if isinstance(publisher_data, dict) else None

    image_data = item.get("image")
    cover_url = image_data.get("medium_url") if isinstance(image_data, dict) else None

    return SeriesSearchResult(
        provider_id=str(item["id"]),
        title=item.get("name", "Unknown"),
        year_start=item_year,
        publisher=publisher_name,
        issue_count=_safe_int(item.get("count_of_issues")),
        status=None,  # ComicVine search doesn't return status
        cover_url=cover_url,
        description=_strip_html(item.get("description") or item.get("deck")),
        comicvine_url=item.get("site_detail_url"),
    )


def _series_metadata_from_item(
    item: dict[str, Any],
    *,
    fallback_provider_id: str,
) -> SeriesMetadata:
    publisher_data = item.get("publisher")
    publisher_name = publisher_data.get("name") if isinstance(publisher_data, dict) else None
    image_data = item.get("image")
    cover_url = image_data.get("medium_url") if isinstance(image_data, dict) else None
    title = item.get("name", "Unknown")
    return SeriesMetadata(
        provider_id=str(item.get("id", fallback_provider_id)),
        title=title,
        sort_title=_make_sort_title(title),
        year_start=_safe_int(item.get("start_year")),
        year_end=None,
        status=None,
        publisher=publisher_name,
        description=_strip_html(item.get("description") or item.get("deck")),
        cover_url=cover_url,
        issue_count=_safe_int(item.get("count_of_issues")),
        comicvine_url=item.get("site_detail_url"),
    )


def _issue_summary_from_item(item: dict[str, Any]) -> IssueSummary:
    image_data = item.get("image")
    cover_url = image_data.get("medium_url") if isinstance(image_data, dict) else None
    title = item.get("name")
    issue_number, issue_number_text = _parse_issue_number_fields(item.get("issue_number"))
    return IssueSummary(
        provider_id=str(item["id"]),
        issue_number=issue_number,
        title=title,
        release_date=item.get("cover_date"),
        cover_url=cover_url,
        issue_type=detect_issue_type(str(title or "")),
        issue_number_text=issue_number_text,
    )


def _issue_metadata_from_item(
    item: dict[str, Any],
    *,
    fallback_provider_id: str,
) -> IssueMetadata:
    volume_data = item.get("volume")
    series_provider_id = str(volume_data["id"]) if isinstance(volume_data, dict) else ""
    image_data = item.get("image")
    cover_url = image_data.get("medium_url") if isinstance(image_data, dict) else None
    issue_number, issue_number_text = _parse_issue_number_fields(item.get("issue_number"))
    return IssueMetadata(
        provider_id=str(item.get("id", fallback_provider_id)),
        series_provider_id=series_provider_id,
        issue_number=issue_number,
        title=item.get("name"),
        description=_strip_html(item.get("description") or item.get("deck")),
        release_date=item.get("cover_date"),
        store_date=item.get("store_date"),
        cover_url=cover_url,
        page_count=None,
        comicvine_url=item.get("site_detail_url"),
        creators=_extract_creators(item.get("person_credits")),
        story_arcs=_extract_story_arcs(item.get("story_arc_credits")),
        issue_number_text=issue_number_text,
    )


def _normalize_bulk_provider_ids(provider_ids: Sequence[str]) -> list[str]:
    if isinstance(provider_ids, (str, bytes)) or len(provider_ids) > _MAX_BULK_PROVIDER_IDS:
        raise ValueError("ComicVine batch requests require at most 5000 provider IDs")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in provider_ids:
        provider_id = str(value).strip()
        if re.fullmatch(r"[1-9][0-9]{0,18}", provider_id) is None:
            raise ValueError("ComicVine provider IDs must be positive integers")
        if provider_id not in seen:
            seen.add(provider_id)
            normalized.append(provider_id)
    return normalized


def _discard_global_series_search_inflight(
    key: _GlobalSeriesSearchInflightKey,
    task: asyncio.Future[tuple[tuple[SeriesSearchResult, ...], int]],
) -> None:
    if _GLOBAL_SERIES_SEARCH_INFLIGHT.get(key) is task:
        _GLOBAL_SERIES_SEARCH_INFLIGHT.pop(key, None)


def _make_sort_title(title: str) -> str:
    """Generate a sort-friendly title by moving leading articles to the end."""
    for article in ("The ", "A ", "An "):
        if title.startswith(article):
            return f"{title[len(article) :]}, {article.strip()}"
    return title


def _extract_creators(person_credits: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Extract creator names and roles from ComicVine person_credits."""
    if not person_credits:
        return []
    creators: list[dict[str, str]] = []
    for person in person_credits:
        name = person.get("name", "")
        role = person.get("role", "")
        if name:
            creator: dict[str, str] = {"name": name, "role": role}
            if person.get("id") is not None:
                creator["provider_id"] = str(person["id"])
            if person.get("site_detail_url"):
                creator["comicvine_url"] = str(person["site_detail_url"])
            creators.append(creator)
    return creators


def _extract_story_arcs(arc_credits: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Extract story arc names and IDs from ComicVine story_arc_credits."""
    if not arc_credits:
        return []
    arcs: list[dict[str, str]] = []
    for arc in arc_credits:
        name = arc.get("name", "")
        arc_id = str(arc.get("id", ""))
        if name:
            arcs.append({"name": name, "provider_id": arc_id})
    return arcs


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ComicVineError(Exception):
    """Raised when the ComicVine API returns a non-OK status."""

    def __init__(self, status_code: int, message: str, *, retryable: bool = False) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ComicVineProvider:
    """ComicVine implementation of MetadataProvider.

    Args:
        api_key: ComicVine API key.
        rate_limit: Maximum requests per hour (default 200).
    """

    def __init__(
        self,
        api_key: str,
        rate_limit: int = 200,
        *,
        burst_limit: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "Pullbox/1.0"},
        )
        normalized_rate_limit = max(int(rate_limit), 1)
        normalized_burst_limit = 1 if burst_limit is None else max(1, min(int(burst_limit), 10))
        self._rate_limit = normalized_rate_limit
        self._requests_per_second = normalized_burst_limit
        self._rate_coordinator: _ComicVineRateCoordinator | None = None

    @property
    def name(self) -> str:
        return "comicvine"

    # -- internal request plumbing ------------------------------------------

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a rate-limited GET request to the ComicVine API."""
        if self._rate_coordinator is None:
            self._rate_coordinator = _rate_coordinator_for(
                api_key=self._api_key,
                rate_limit=self._rate_limit,
                requests_per_second=self._requests_per_second,
            )
        await self._rate_coordinator.acquire(
            _resource_rate_key(endpoint),
            requests_per_second=self._requests_per_second,
        )

        request_params: dict[str, Any] = {
            "api_key": self._api_key,
            "format": "json",
        }
        if params:
            request_params.update(params)

        log = logger.bind(endpoint=endpoint)
        log.debug(
            "comicvine_request",
            params={k: v for k, v in request_params.items() if k != "api_key"},
        )

        try:
            response = await self._client.get(endpoint, params=request_params)
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("comicvine_timeout")
            raise ComicVineError(
                0,
                f"Request timed out: {endpoint}",
                retryable=True,
            ) from None
        except httpx.HTTPStatusError as exc:
            http_status = exc.response.status_code
            if http_status == 404:
                log.warning("comicvine_not_found")
                raise ComicVineError(_STATUS_NOT_FOUND, f"Resource not found: {endpoint}") from None
            log.error("comicvine_http_error", status=http_status)
            raise ComicVineError(
                http_status,
                f"HTTP {http_status}: {endpoint}",
                retryable=http_status in {408, 420, 429} or http_status >= 500,
            ) from None
        except httpx.HTTPError as exc:
            log.error("comicvine_request_failed", error=str(exc))
            raise ComicVineError(0, f"Request failed: {exc}", retryable=True) from None

        data: dict[str, Any] = response.json()
        status_code = data.get("status_code", 0)

        if status_code == _STATUS_INVALID_KEY:
            log.error("comicvine_invalid_api_key")
            raise ComicVineError(status_code, "Invalid API key")
        if status_code == _STATUS_NOT_FOUND:
            log.warning("comicvine_not_found")
            raise ComicVineError(status_code, f"Resource not found: {endpoint}")
        if status_code == _STATUS_RATE_LIMITED:
            log.warning("comicvine_rate_limited")
            raise ComicVineError(status_code, "Rate limit exceeded", retryable=True)
        if status_code != _STATUS_OK:
            error_msg = data.get("error", "Unknown error")
            log.error("comicvine_api_error", status_code=status_code, error=error_msg)
            raise ComicVineError(status_code, f"API error {status_code}: {error_msg}")

        log.debug("comicvine_response_ok", total_results=data.get("number_of_total_results"))
        return data

    # -- Optional StoryArcMetadataProvider capability -----------------------

    async def search_story_arcs(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[StoryArcSearchResult]:
        """Search a bounded page of Comic Vine story arcs by name."""
        results, _ = await self.search_story_arcs_page(query, limit=limit, offset=offset)
        return results

    async def search_story_arcs_page(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[StoryArcSearchResult], int]:
        """Return arc-name search results and the provider's arc result count."""
        from pullbox.providers.metadata.comicvine_story_arcs import search_story_arcs_page

        return await search_story_arcs_page(self._request, query, limit=limit, offset=offset)

    async def get_story_arc(self, provider_id: str) -> StoryArcMetadata:
        """Read explicit membership without claiming a curated reading order."""
        from pullbox.providers.metadata.comicvine_story_arcs import get_story_arc

        return await get_story_arc(self._request, provider_id)

    async def get_story_arc_issues(self, issue_provider_ids: Sequence[str]) -> list[IssueMetadata]:
        """Hydrate the exact unique issue set in requested order."""
        from pullbox.providers.metadata.comicvine_story_arcs import get_story_arc_issues

        return await get_story_arc_issues(self._request, issue_provider_ids)

    # -- MetadataProvider implementation ------------------------------------

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> list[SeriesSearchResult]:
        """Search for series (volumes) by title and optional start year."""
        results, _total_results = await self.search_series_page(
            query,
            year,
            limit=limit,
            offset=offset,
            suppress_errors=suppress_errors,
        )
        return results

    async def search_series_page(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        """Search for series and return the current page plus ComicVine's total."""
        log = logger.bind(query=query, year=year, limit=limit, offset=offset)
        log.debug("comicvine_search_series")

        params: dict[str, Any] = {
            "resources": "volume",
            "query": query,
            "field_list": (
                "id,name,start_year,publisher,count_of_issues,image,"
                "description,deck,site_detail_url"
            ),
            "limit": limit,
            "page": (offset // limit) + 1,
        }

        try:
            data = await self._request("/search/", params)
        except ComicVineError:
            log.exception("comicvine_search_failed")
            if suppress_errors:
                return [], 0
            raise

        total_results = _safe_int(data.get("number_of_total_results")) or 0

        results: list[SeriesSearchResult] = []
        for item in data.get("results", []):
            results.append(_series_search_result_from_item(item))

        # When a year is specified, sort year-matching results to the top
        # rather than discarding non-matches (ComicVine search doesn't
        # support server-side year filtering).
        if year:
            results.sort(key=lambda r: (r.year_start != year, r.year_start is None))

        log.debug("comicvine_search_results", count=len(results), total_results=total_results)
        return results, total_results

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = _GLOBAL_SERIES_SEARCH_MAX_RESULTS,
        batch_size: int = _GLOBAL_SERIES_SEARCH_BATCH_SIZE,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        """Fetch a full volume candidate set for global local sorting.

        ComicVine's generic ``/search/`` endpoint returns relevance-ordered
        pages, which means a local year sort can only sort the current page.
        This mirrors Mylar's approach: query the ``/volumes/`` resource with
        name filters, walk ComicVine offsets in 100-result batches, then let
        callers sort and paginate the accumulated candidate set locally.
        """
        normalized_query = " ".join(query.strip().split())
        if not normalized_query:
            return [], 0

        normalized_max = max(1, int(max_results))
        normalized_batch = min(max(1, int(batch_size)), _GLOBAL_SERIES_SEARCH_BATCH_SIZE)
        normalized_batch = min(normalized_batch, normalized_max)
        filter_value = _comicvine_name_filter(normalized_query)
        if not filter_value:
            return [], 0

        cache_key = (normalized_query.casefold(), normalized_max, normalized_batch)
        now = time.monotonic()
        cached = _GLOBAL_SERIES_SEARCH_CACHE.get(cache_key)
        if cached is not None and now - cached[0] <= _GLOBAL_SERIES_SEARCH_CACHE_TTL_SECONDS:
            cached_results, cached_total = cached[1], cached[2]
            return list(cached_results), cached_total

        expired_keys = [
            key
            for key, (fetched_at, _results, _total) in _GLOBAL_SERIES_SEARCH_CACHE.items()
            if now - fetched_at > _GLOBAL_SERIES_SEARCH_CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            _GLOBAL_SERIES_SEARCH_CACHE.pop(key, None)

        inflight_key = (*cache_key, suppress_errors)
        inflight = _GLOBAL_SERIES_SEARCH_INFLIGHT.get(inflight_key)
        if inflight is None:
            inflight = asyncio.create_task(
                self._fetch_global_series_search(
                    normalized_query,
                    filter_value,
                    normalized_max,
                    normalized_batch,
                    cache_key,
                    suppress_errors=suppress_errors,
                )
            )
            _GLOBAL_SERIES_SEARCH_INFLIGHT[inflight_key] = inflight

            def discard_finished_search(
                task: asyncio.Future[tuple[tuple[SeriesSearchResult, ...], int]],
            ) -> None:
                _discard_global_series_search_inflight(inflight_key, task)

            inflight.add_done_callback(discard_finished_search)

        try:
            result_tuple, effective_total = await asyncio.shield(inflight)
        finally:
            if inflight.done() and _GLOBAL_SERIES_SEARCH_INFLIGHT.get(inflight_key) is inflight:
                _GLOBAL_SERIES_SEARCH_INFLIGHT.pop(inflight_key, None)

        return list(result_tuple), effective_total

    async def _fetch_global_series_search(
        self,
        normalized_query: str,
        filter_value: str,
        normalized_max: int,
        normalized_batch: int,
        cache_key: tuple[str, int, int],
        *,
        suppress_errors: bool,
    ) -> tuple[tuple[SeriesSearchResult, ...], int]:
        """Fetch and cache a global volume search candidate set."""
        log = logger.bind(
            query=normalized_query,
            max_results=normalized_max,
            batch_size=normalized_batch,
        )
        log.debug("comicvine_global_series_search")

        params_base: dict[str, Any] = {
            "filter": filter_value,
            "field_list": (
                "id,name,start_year,publisher,count_of_issues,image,"
                "description,deck,site_detail_url"
            ),
            "sort": "date_last_updated:desc",
        }

        results: list[SeriesSearchResult] = []
        seen_ids: set[str] = set()
        total_results = 0
        offset = 0

        while offset < normalized_max:
            limit = min(normalized_batch, normalized_max - offset)
            params = {
                **params_base,
                "limit": limit,
                "offset": offset,
            }
            try:
                data = await self._request("/volumes/", params)
            except ComicVineError:
                log.exception("comicvine_global_series_search_failed")
                if suppress_errors:
                    return (), 0
                raise

            if offset == 0:
                total_results = _safe_int(data.get("number_of_total_results")) or 0

            items = list(data.get("results", []))
            if not items:
                break

            for item in items:
                result = _series_search_result_from_item(item)
                if result.provider_id in seen_ids:
                    continue
                seen_ids.add(result.provider_id)
                results.append(result)

            if len(items) < limit:
                break

            effective_total = (
                min(total_results, normalized_max) if total_results else normalized_max
            )
            if offset + limit >= effective_total:
                break

            offset += limit

        effective_total = min(total_results, normalized_max) if total_results else len(results)
        effective_total = min(effective_total, len(results))
        result_tuple = tuple(results)
        _GLOBAL_SERIES_SEARCH_CACHE[cache_key] = (
            time.monotonic(),
            result_tuple,
            effective_total,
        )
        log.debug(
            "comicvine_global_series_search_results",
            count=len(results),
            total_results=effective_total,
        )
        return result_tuple, effective_total

    async def get_series(self, provider_id: str) -> SeriesMetadata:
        """Get full series (volume) metadata by ComicVine volume ID."""
        log = logger.bind(provider_id=provider_id)
        log.debug("comicvine_get_series")

        params: dict[str, Any] = {
            "field_list": (
                "id,name,start_year,count_of_issues,publisher,"
                "image,description,deck,site_detail_url"
            ),
        }

        data = await self._request(f"/volume/{_VOLUME_PREFIX}-{provider_id}/", params)
        item: dict[str, Any] = data.get("results", {})

        return _series_metadata_from_item(item, fallback_provider_id=provider_id)

    async def get_series_batch(self, provider_ids: Sequence[str]) -> dict[str, SeriesMetadata]:
        """Fetch volume profiles in bounded ID-filter batches."""
        normalized = _normalize_bulk_provider_ids(provider_ids)
        found: dict[str, SeriesMetadata] = {}
        field_list = (
            "id,name,start_year,count_of_issues,publisher,image,description,deck,site_detail_url"
        )
        for start in range(0, len(normalized), _BULK_RESOURCE_PAGE_SIZE):
            batch = normalized[start : start + _BULK_RESOURCE_PAGE_SIZE]
            data = await self._request(
                "/volumes/",
                {
                    "filter": f"id:{'|'.join(batch)}",
                    "field_list": field_list,
                    "limit": _BULK_RESOURCE_PAGE_SIZE,
                    "offset": 0,
                },
            )
            for raw_item in data.get("results", []):
                item = raw_item if isinstance(raw_item, dict) else {}
                metadata = _series_metadata_from_item(item, fallback_provider_id="")
                if metadata.provider_id in batch:
                    found[metadata.provider_id] = metadata
        return {
            provider_id: found[provider_id] for provider_id in normalized if provider_id in found
        }

    async def get_issue(self, provider_id: str) -> IssueMetadata:
        """Get full issue metadata by ComicVine issue ID."""
        log = logger.bind(provider_id=provider_id)
        log.debug("comicvine_get_issue")

        params: dict[str, Any] = {
            "field_list": (
                "id,volume,issue_number,name,description,deck,"
                "cover_date,store_date,image,site_detail_url,"
                "person_credits,story_arc_credits"
            ),
        }

        data = await self._request(f"/issue/{_ISSUE_PREFIX}-{provider_id}/", params)
        item: dict[str, Any] = data.get("results", {})

        return _issue_metadata_from_item(item, fallback_provider_id=provider_id)

    async def get_issue_batch(self, provider_ids: Sequence[str]) -> dict[str, IssueMetadata]:
        """Fetch full issue metadata in bounded ID-filter batches."""
        normalized = _normalize_bulk_provider_ids(provider_ids)
        found: dict[str, IssueMetadata] = {}
        field_list = (
            "id,volume,issue_number,name,description,deck,cover_date,store_date,"
            "image,site_detail_url,person_credits,story_arc_credits"
        )
        for start in range(0, len(normalized), _BULK_RESOURCE_PAGE_SIZE):
            batch = normalized[start : start + _BULK_RESOURCE_PAGE_SIZE]
            data = await self._request(
                "/issues/",
                {
                    "filter": f"id:{'|'.join(batch)}",
                    "field_list": field_list,
                    "limit": _BULK_RESOURCE_PAGE_SIZE,
                    "offset": 0,
                },
            )
            for raw_item in data.get("results", []):
                item = raw_item if isinstance(raw_item, dict) else {}
                metadata = _issue_metadata_from_item(item, fallback_provider_id="")
                if metadata.provider_id in batch:
                    found[metadata.provider_id] = metadata
        return {
            provider_id: found[provider_id] for provider_id in normalized if provider_id in found
        }

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        """Get all issues for a series (volume), paginating automatically."""
        log = logger.bind(series_provider_id=series_provider_id)
        log.debug("comicvine_get_issues_for_series")

        all_issues: list[IssueSummary] = []
        offset = 0
        limit = 100

        while True:
            params: dict[str, Any] = {
                "filter": f"volume:{series_provider_id}",
                "field_list": "id,issue_number,name,cover_date,image",
                "sort": "issue_number:asc",
                "limit": limit,
                "offset": offset,
            }

            try:
                data = await self._request("/issues/", params)
            except ComicVineError:
                log.exception("comicvine_get_issues_failed", offset=offset)
                raise

            for item in data.get("results", []):
                all_issues.append(_issue_summary_from_item(item))

            total = data.get("number_of_total_results", 0)
            offset += limit
            if offset >= total:
                break

        log.debug("comicvine_issues_fetched", count=len(all_issues))
        return all_issues

    async def get_issue_catalog_batch(
        self,
        series_provider_ids: Sequence[str],
    ) -> dict[str, list[IssueSummary]]:
        """Fetch and group issue summaries for multiple volumes."""
        normalized = _normalize_bulk_provider_ids(series_provider_ids)
        grouped: dict[str, list[IssueSummary]] = {provider_id: [] for provider_id in normalized}
        for start in range(0, len(normalized), _BULK_RESOURCE_PAGE_SIZE):
            batch = normalized[start : start + _BULK_RESOURCE_PAGE_SIZE]
            offset = 0
            while True:
                data = await self._request(
                    "/issues/",
                    {
                        "filter": f"volume:{'|'.join(batch)}",
                        "field_list": "id,volume,issue_number,name,cover_date,image",
                        "sort": "issue_number:asc",
                        "limit": _BULK_RESOURCE_PAGE_SIZE,
                        "offset": offset,
                    },
                )
                items = data.get("results", [])
                for raw_item in items:
                    item = raw_item if isinstance(raw_item, dict) else {}
                    volume = item.get("volume")
                    volume_id = str(volume.get("id")) if isinstance(volume, dict) else ""
                    if volume_id in grouped:
                        grouped[volume_id].append(_issue_summary_from_item(item))
                total = _safe_int(data.get("number_of_total_results")) or 0
                offset += len(items)
                if not items or offset >= total:
                    break
        return grouped

    async def get_recent_issues_for_series(
        self,
        series_provider_id: str,
        *,
        limit: int = 100,
    ) -> list[IssueSummary]:
        """Get the newest issue summaries for a series with a single API request."""
        normalized_limit = min(max(1, int(limit)), 100)
        log = logger.bind(series_provider_id=series_provider_id, limit=normalized_limit)
        log.debug("comicvine_get_recent_issues_for_series")

        params: dict[str, Any] = {
            "filter": f"volume:{series_provider_id}",
            "field_list": "id,issue_number,name,cover_date,store_date,image",
            "sort": "store_date:desc",
            "limit": normalized_limit,
            "offset": 0,
        }

        try:
            data = await self._request("/issues/", params)
        except ComicVineError:
            log.exception("comicvine_get_recent_issues_failed")
            raise

        summaries = [_issue_summary_from_item(item) for item in data.get("results", [])]
        log.debug("comicvine_recent_issues_fetched", count=len(summaries))
        return summaries

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: list[float],
    ) -> list[IssueSummary]:
        """Get selected issues for a series by issue number."""
        log = logger.bind(
            series_provider_id=series_provider_id,
            issue_numbers=issue_numbers,
        )
        log.debug("comicvine_get_issues_for_series_by_numbers")

        summaries: list[IssueSummary] = []
        seen_numbers = sorted({float(number) for number in issue_numbers})
        for issue_number in seen_numbers:
            params: dict[str, Any] = {
                "filter": (
                    f"volume:{series_provider_id},"
                    f"issue_number:{_format_issue_number_filter(issue_number)}"
                ),
                "field_list": "id,issue_number,name,cover_date,image",
                "sort": "issue_number:asc",
                "limit": 100,
                "offset": 0,
            }
            try:
                data = await self._request("/issues/", params)
            except ComicVineError:
                log.exception("comicvine_get_issues_by_number_failed", issue_number=issue_number)
                raise

            for item in data.get("results", []):
                summaries.append(_issue_summary_from_item(item))

        log.debug("comicvine_targeted_issues_fetched", count=len(summaries))
        return summaries

    async def get_cover_image(self, image_url: str) -> bytes:
        """Download a cover image from ComicVine CDN. Returns raw bytes.

        Cover URLs are direct CDN links — no API key or rate limiting needed.
        """
        log = logger.bind(image_url=image_url)
        log.debug("comicvine_download_cover")

        try:
            response = await self._client.get(image_url)
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("comicvine_cover_timeout")
            raise ComicVineError(0, f"Cover download timed out: {image_url}") from None
        except httpx.HTTPError as exc:
            log.error("comicvine_cover_failed", error=str(exc))
            raise ComicVineError(0, f"Cover download failed: {exc}") from None

        log.debug("comicvine_cover_downloaded", size_bytes=len(response.content))
        return response.content

    async def test_connection(self) -> ProviderHealthResult:
        """Validate the API key with a lightweight search request."""
        start = time.monotonic()
        try:
            await self._request(
                "/search/",
                {"resources": "volume", "query": "test", "limit": 1},
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=True,
                message="ComicVine API key is valid",
                response_time_ms=elapsed_ms,
            )
        except ComicVineError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=False,
                message=f"ComicVine API error: {exc}",
                response_time_ms=elapsed_ms,
                details={"status_code": str(exc.status_code)},
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("comicvine_test_connection_failed")
            return ProviderHealthResult(
                healthy=False,
                message=f"Connection failed: {exc}",
                response_time_ms=elapsed_ms,
            )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
