"""ComicVine search helpers used by import series matching."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from pullbox.core.exceptions import ImportProviderDegradedError, RateLimitError
from pullbox.core.name_matcher import NameMatcher
from pullbox.providers.metadata.comicvine import ComicVineError

if TYPE_CHECKING:
    from pullbox.providers.base import SeriesSearchResult
    from pullbox.providers.metadata.comicvine import ComicVineProvider

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_IMPORT_SEARCH_LIMIT = 10
_IMPORT_GLOBAL_SEARCH_LIMIT = 1000


async def search_with_retry(
    provider: ComicVineProvider,
    query: str,
    year: int | None,
) -> list[SeriesSearchResult]:
    """Search ComicVine with retry on transient provider failures."""
    if getattr(provider, "is_local_catalog", False) is True:
        results, _ = await provider.search_series_globally(
            query, max_results=_IMPORT_GLOBAL_SEARCH_LIMIT
        )
        return list(results)
    last_provider_error: str | None = None
    saw_successful_response = False
    for attempt in range(_MAX_RETRIES):
        try:
            results = await _search_series_once(provider, query, year)
            saw_successful_response = True
            if results:
                return results
            wait = min(2**attempt, 4)
            logger.warning(
                "import_cv_empty_results",
                query=query,
                attempt=attempt + 1,
                retry_after=wait,
            )
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(wait)
                continue
            return []
        except RateLimitError as e:
            wait = e.retry_after_seconds or (2**attempt)
            last_provider_error = str(e)
            logger.warning(
                "import_cv_rate_limited",
                query=query,
                attempt=attempt + 1,
                retry_after=wait,
            )
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(min(wait, 10))
                continue
            break
        except ComicVineError as exc:
            if exc.status_code == 107:
                wait = min(2**attempt, 10)
                last_provider_error = str(exc)
                logger.warning(
                    "import_cv_rate_limited",
                    query=query,
                    attempt=attempt + 1,
                    retry_after=wait,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
                    continue
                break
            if exc.status_code == 0:
                wait = min(2**attempt, 4)
                last_provider_error = str(exc)
                logger.warning(
                    "import_cv_transient_error",
                    query=query,
                    attempt=attempt + 1,
                    retry_after=wait,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
                    continue
                break
            return []
        except httpx.TimeoutException:
            wait = min(2**attempt, 4)
            last_provider_error = "Request timed out"
            logger.warning(
                "import_cv_timeout",
                query=query,
                attempt=attempt + 1,
                retry_after=wait,
            )
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(wait)
                continue
            break
        except Exception:
            logger.warning("import_cv_search_error", query=query, exc_info=True)
            last_provider_error = "Unexpected search error"
            break
    if last_provider_error is not None and not saw_successful_response:
        raise ImportProviderDegradedError(
            provider="comicvine",
            query=query,
            year=year,
            attempts=_MAX_RETRIES,
            last_error=last_provider_error,
        )
    return []


async def _search_series_once(
    provider: ComicVineProvider,
    query: str,
    year: int | None,
) -> list[SeriesSearchResult]:
    """Fetch import series candidates, preferring the global ComicVine volume search."""
    global_results: list[SeriesSearchResult] = []
    global_search = _explicit_provider_method(provider, "search_series_globally")
    if global_search is not None:
        try:
            global_response = await global_search(
                query,
                max_results=_IMPORT_GLOBAL_SEARCH_LIMIT,
                suppress_errors=False,
            )
        except TypeError:
            try:
                global_response = await global_search(
                    query,
                    max_results=_IMPORT_GLOBAL_SEARCH_LIMIT,
                )
            except TypeError:
                global_response = await global_search(query)
        global_results = _series_results_from_global_response(global_response)
        if global_results and _has_compact_exact_series_result(query, global_results):
            return global_results

    try:
        page_results = await provider.search_series(
            query,
            year,
            limit=_IMPORT_SEARCH_LIMIT,
            suppress_errors=False,
        )
    except TypeError:
        try:
            page_results = await provider.search_series(
                query,
                year,
                limit=_IMPORT_SEARCH_LIMIT,
            )
        except TypeError:
            page_results = await provider.search_series(query, year)

    if global_results and page_results:
        return _merge_series_results(global_results, page_results)
    if page_results:
        return page_results
    return global_results


def _explicit_provider_method(provider: Any, method_name: str) -> Any | None:
    """Return a provider method only when the concrete provider explicitly exposes it."""
    method = getattr(provider, method_name, None)
    if not callable(method):
        return None
    if not hasattr(type(provider), method_name):
        return None
    return method


def _series_results_from_global_response(response: Any) -> list[SeriesSearchResult]:
    if isinstance(response, tuple) and len(response) == 2 and isinstance(response[0], list):
        return list(response[0])
    if isinstance(response, list):
        return response
    return []


def _compact_normalized_title(value: str) -> str:
    """Normalize title text and remove spacing for alphanumeric-boundary aliases."""
    return NameMatcher.normalize(value).replace(" ", "")


def _has_compact_exact_series_result(query: str, results: list[SeriesSearchResult]) -> bool:
    """Return true when global search already includes an exact title candidate."""
    compact_query = _compact_normalized_title(query)
    if not compact_query:
        return False
    return any(_compact_normalized_title(result.title) == compact_query for result in results)


def _merge_series_results(
    primary_results: list[SeriesSearchResult],
    fallback_results: list[SeriesSearchResult],
) -> list[SeriesSearchResult]:
    """Merge ComicVine candidate lists, preserving first-seen provider IDs."""
    merged: list[SeriesSearchResult] = []
    seen_ids: set[str] = set()
    for result in [*primary_results, *fallback_results]:
        provider_id = str(result.provider_id)
        if provider_id in seen_ids:
            continue
        seen_ids.add(provider_id)
        merged.append(result)
    return merged
