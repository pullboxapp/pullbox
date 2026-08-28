"""Prowlarr indexer integration.

Uses the Prowlarr API to search across all configured indexers at once.
Prowlarr returns results in Newznab/Torznab XML format, so we reuse
the existing XML parsing from the Newznab indexer.

Prowlarr API docs: https://prowlarr.com/docs/api/
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import (
    IndexerCapabilities,
    ProviderHealthResult,
    ReleaseResult,
    SearchQuery,
)
from pullbox.providers.indexer.newznab import NewznabIndexer

logger = structlog.get_logger(__name__)

# Per-indexer requests (health, capabilities) can use a shorter timeout.
# Aggregate searches fan out to many indexers server-side and need more time.
_REQUEST_TIMEOUT = 15.0
_SEARCH_TIMEOUT = 45.0


class ProwlarrError(Exception):
    """Raised when the Prowlarr API returns an error."""


class ProwlarrIndexer:
    """Prowlarr implementation of the Indexer protocol.

    Searches across all indexers configured in a Prowlarr instance.
    Results are returned in Newznab/Torznab format and parsed using
    the shared Newznab XML parser.

    Args:
        url: Base URL of the Prowlarr instance (e.g. ``http://localhost:9696``).
        api_key: Prowlarr API key.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        indexer_ids: list[int] | None = None,
        indexer_rankings: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._indexer_ids = indexer_ids
        self._indexer_rankings = indexer_rankings or {}
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
        # Reuse Newznab XML parsing via a lightweight instance
        self._xml_parser = NewznabIndexer(name="prowlarr", url=url, api_key=api_key)

    @property
    def name(self) -> str:
        return "prowlarr"

    @property
    def indexer_type(self) -> str:
        return "prowlarr"

    @property
    def supports_nzb(self) -> bool:
        return True

    @property
    def supports_torrent(self) -> bool:
        return True

    # -- internal request plumbing ------------------------------------------

    async def _api_request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Make an authenticated GET request to the Prowlarr REST API."""
        url = f"{self._base_url}/api/v1{endpoint}"
        headers = {"X-Api-Key": self._api_key}

        log = logger.bind(endpoint=endpoint)
        log.debug("prowlarr_request")

        try:
            response = await self._client.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("prowlarr_timeout")
            raise ProwlarrError(f"Request timed out: {endpoint}") from None
        except httpx.HTTPStatusError as exc:
            log.error("prowlarr_http_error", status=exc.response.status_code)
            raise ProwlarrError(f"HTTP {exc.response.status_code}: {endpoint}") from None
        except httpx.HTTPError as exc:
            log.error("prowlarr_request_failed", error=str(exc))
            raise ProwlarrError(f"Request failed: {exc}") from None

        return response.json()

    async def _newznab_request(self, params: dict[str, Any]) -> str:
        """Make a Newznab-style request to Prowlarr's Newznab endpoint."""
        url = f"{self._base_url}/api/v1/indexer/1/newznab"
        request_params = {"apikey": self._api_key, **params}

        log = logger.bind(function=params.get("t"))
        log.debug("prowlarr_newznab_request")

        try:
            response = await self._client.get(url, params=request_params)
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("prowlarr_newznab_timeout")
            raise ProwlarrError("Newznab request timed out") from None
        except httpx.HTTPError as exc:
            log.error("prowlarr_newznab_failed", error=str(exc))
            raise ProwlarrError(f"Newznab request failed: {exc}") from None

        return response.text

    # -- Indexer implementation ---------------------------------------------

    async def search(self, query: SearchQuery) -> list[ReleaseResult]:
        """Search across all Prowlarr indexers via the REST API."""
        log = logger.bind(
            series=query.series_title,
            issue=query.issue_number,
        )
        log.debug("prowlarr_search")

        search_term = query.series_title
        if query.issue_number is not None:
            issue_str = (
                str(int(query.issue_number))
                if query.issue_number == int(query.issue_number)
                else str(query.issue_number)
            )
            search_term = f"{search_term} {issue_str}"

        params: dict[str, Any] = {"query": search_term, "type": "search"}
        if query.categories:
            params["categories"] = [int(c) for c in query.categories]
        if self._indexer_ids:
            params["indexerIds"] = self._indexer_ids

        data = await self._api_request("/search", params, timeout=_SEARCH_TIMEOUT)

        results = self._parse_api_results(data, self._indexer_rankings)
        log.debug("prowlarr_search_results", count=len(results))
        return results

    async def get_capabilities(self) -> IndexerCapabilities:
        """Get aggregate capabilities from all Prowlarr indexers."""
        log = logger.bind()
        log.debug("prowlarr_get_capabilities")

        try:
            indexers = await self._api_request("/indexer")
        except ProwlarrError:
            log.exception("prowlarr_capabilities_failed")
            return IndexerCapabilities()

        categories: list[str] = []
        seen_cats: set[str] = set()
        for indexer in indexers:
            for cap in indexer.get("capabilities", {}).get("categories", []):
                cat_id = str(cap.get("id", ""))
                cat_name = cap.get("name", "")
                key = f"{cat_id}:{cat_name}"
                if key not in seen_cats and cat_id and cat_name:
                    categories.append(key)
                    seen_cats.add(key)

        search_params = ["search"]
        return IndexerCapabilities(
            categories=categories,
            search_params=search_params,
        )

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity by listing configured indexers."""
        start = time.monotonic()
        try:
            indexers = await self._api_request("/indexer")
            elapsed_ms = (time.monotonic() - start) * 1000
            count = len(indexers)
            return ProviderHealthResult(
                healthy=True,
                message=f"Prowlarr: {count} indexer(s) configured",
                response_time_ms=elapsed_ms,
                details={"indexer_count": str(count)},
            )
        except ProwlarrError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=False,
                message=f"Prowlarr error: {exc}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("prowlarr_test_connection_failed")
            return ProviderHealthResult(
                healthy=False,
                message=f"Connection failed: {exc}",
                response_time_ms=elapsed_ms,
            )

    async def get_indexers(self) -> list[dict[str, Any]]:
        """List all indexers configured in Prowlarr."""
        result: list[dict[str, Any]] = await self._api_request("/indexer")
        return result

    async def close(self) -> None:
        """Close the underlying HTTP clients."""
        await self._client.aclose()
        await self._xml_parser.close()

    # -- result parsing -----------------------------------------------------

    @staticmethod
    def _parse_api_results(
        data: list[dict[str, Any]],
        indexer_rankings: dict[int, tuple[int, int]] | None = None,
    ) -> list[ReleaseResult]:
        """Parse Prowlarr REST API search results into ReleaseResult DTOs."""
        results: list[ReleaseResult] = []
        rankings = indexer_rankings or {}
        for item in data:
            indexer_name = item.get("indexer", "Unknown")
            is_torrent = item.get("protocol", "").lower() == "torrent"

            download_url = item.get("downloadUrl") or item.get("guid", "")
            published_at = _parse_iso_date(item.get("publishDate"))

            from pullbox.providers.indexer.newznab import _calc_age_days

            info_url = item.get("infoUrl") or item.get("commentUrl")
            if not info_url:
                guid = item.get("guid", "")
                info_url = guid if guid.startswith("http") else None

            raw_indexer_id = item.get("indexerId")
            try:
                remote_indexer_id = int(raw_indexer_id) if raw_indexer_id is not None else None
            except (TypeError, ValueError):
                remote_indexer_id = None
            local_indexer_id: int | None = None
            ranking_priority = 25
            if remote_indexer_id is not None:
                ranking = rankings.get(remote_indexer_id)
                if ranking is not None:
                    local_indexer_id, ranking_priority = ranking

            results.append(
                ReleaseResult(
                    title=item.get("title", "Unknown"),
                    indexer_name=indexer_name,
                    download_url=download_url,
                    size_bytes=item.get("size"),
                    age_days=_calc_age_days(published_at),
                    seeders=item.get("seeders") if is_torrent else None,
                    leechers=item.get("leechers") if is_torrent else None,
                    grabs=item.get("grabs"),
                    protocol=(
                        AcquisitionProtocol.TORRENT if is_torrent else AcquisitionProtocol.USENET
                    ),
                    category=_join_categories(item.get("categories", [])),
                    published_at=published_at,
                    info_url=info_url,
                    indexer_id=local_indexer_id,
                    ranking_priority=ranking_priority,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_date(value: str | None) -> Any:
    """Parse an ISO 8601 date string to a datetime."""
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _join_categories(categories: list[dict[str, Any]]) -> str | None:
    """Join all category names from Prowlarr's category list.

    Returns comma-separated names so that multi-category results can be
    correctly filtered (e.g. a result tagged both "Books/Comics" and
    "Movies/HD" will be recognized as comic-related).
    """
    names = [c.get("name", "") for c in categories if c.get("name")]
    return ",".join(names) if names else None
