"""Newznab indexer implementation.

Implements the Indexer protocol for Newznab-compatible Usenet indexers.
Provides the shared base logic (XML parsing, capabilities, search) that
the Torznab indexer also builds on.

Newznab API spec: https://newznab.readthedocs.io/en/latest/misc/api/
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx
import structlog
from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import (
    IndexerCapabilities,
    ProviderHealthResult,
    ReleaseResult,
    SearchQuery,
)

logger = structlog.get_logger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0

# Newznab / Torznab XML namespaces
# noinspection HttpUrlsUsage — XML namespace URIs, not HTTP links
_NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
_TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
_NS = {"newznab": _NEWZNAB_NS, "torznab": _TORZNAB_NS}


class NewznabError(Exception):
    """Raised when a Newznab API request fails."""


class NewznabIndexer:
    """Newznab implementation of the Indexer protocol.

    Serves as both a standalone Usenet indexer and the base for TorznabIndexer.

    Args:
        name: Human-readable indexer name.
        url: Base URL of the Newznab-compatible API.
        api_key: Optional API key for authentication.
        rate_limit_per_minute: Maximum requests per minute (default 5).
    """

    def __init__(
        self,
        name: str,
        url: str,
        api_key: str,
        rate_limit_per_minute: int = 5,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._name = name
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=request_timeout)
        self._min_interval = 60.0 / rate_limit_per_minute
        self._last_request_time = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def indexer_type(self) -> str:
        return "newznab"

    @property
    def supports_nzb(self) -> bool:
        return True

    @property
    def supports_torrent(self) -> bool:
        return False

    # -- rate limiting ------------------------------------------------------

    async def _wait_for_rate_limit(self) -> None:
        """Simple rate limiter: enforce minimum interval between requests."""
        import asyncio

        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    # -- internal request plumbing ------------------------------------------

    async def _request(self, params: dict[str, Any]) -> str:
        """Make a rate-limited GET request, returning raw XML text."""
        await self._wait_for_rate_limit()

        request_params: dict[str, Any] = (
            {"apikey": self._api_key, **params} if self._api_key else dict(params)
        )
        url = f"{self._base_url}/api"

        log = logger.bind(indexer=self._name, function=params.get("t"))
        log.debug("newznab_request")

        try:
            response = await self._client.get(url, params=request_params)
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("newznab_timeout")
            raise NewznabError(f"Request timed out: {self._name}") from None
        except httpx.HTTPStatusError as exc:
            log.error("newznab_http_error", status=exc.response.status_code)
            raise NewznabError(f"HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            log.error("newznab_request_failed", error=str(exc))
            raise NewznabError(f"Request failed: {exc}") from None

        # Check for Newznab XML error responses
        text = response.text
        if "<error " in text:
            _check_xml_error(text, self._name)

        return text

    # -- Indexer implementation ---------------------------------------------

    async def search(self, query: SearchQuery) -> list[ReleaseResult]:
        """Search for releases matching the query."""
        log = logger.bind(
            indexer=self._name,
            series=query.series_title,
            issue=query.issue_number,
        )
        log.debug("newznab_search")

        search_term = query.series_title
        if query.issue_number is not None:
            issue_str = (
                str(int(query.issue_number))
                if query.issue_number == int(query.issue_number)
                else str(query.issue_number)
            )
            search_term = f"{search_term} {issue_str}"

        params: dict[str, Any] = {"t": "search", "q": search_term}
        if query.categories:
            params["cat"] = ",".join(query.categories)

        xml_text = await self._request(params)

        results = self._parse_search_results(xml_text)
        log.debug("newznab_search_results", count=len(results))
        return results

    async def get_capabilities(self) -> IndexerCapabilities:
        """Get indexer capabilities by calling the ?t=caps endpoint."""
        log = logger.bind(indexer=self._name)
        log.debug("newznab_get_capabilities")

        xml_text = await self._request({"t": "caps"})
        return _parse_capabilities(xml_text)

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity by fetching capabilities."""
        start = time.monotonic()
        try:
            caps = await self.get_capabilities()
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=True,
                message=f"{self._name}: {len(caps.categories)} categories available",
                response_time_ms=elapsed_ms,
                details={"categories": str(len(caps.categories))},
            )
        except NewznabError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=False,
                message=f"{self._name} error: {exc}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("newznab_test_connection_failed", indexer=self._name)
            return ProviderHealthResult(
                healthy=False,
                message=f"Connection failed: {exc}",
                response_time_ms=elapsed_ms,
            )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # -- XML parsing --------------------------------------------------------

    def _parse_search_results(self, xml_text: str) -> list[ReleaseResult]:
        """Parse Newznab RSS search results into ReleaseResult DTOs."""
        try:
            root = DefusedElementTree.fromstring(xml_text)
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            logger.error("newznab_xml_parse_error", error=str(exc), indexer=self._name)
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        results: list[ReleaseResult] = []
        for item in channel.findall("item"):
            results.append(self._parse_item(item))
        return results

    def _parse_item(self, item: ElementTree.Element) -> ReleaseResult:
        """Parse a single RSS <item> into a ReleaseResult."""
        title, download_url, size_bytes, attrs, published_at, age_days, info_url = (
            _parse_item_common(item)
        )

        return ReleaseResult(
            title=title,
            indexer_name=self._name,
            download_url=download_url,
            size_bytes=size_bytes,
            age_days=age_days,
            seeders=None,
            leechers=None,
            grabs=_safe_int(attrs.get("grabs")),
            protocol=AcquisitionProtocol.USENET,
            category=attrs.get("category"),
            published_at=published_at,
            info_url=info_url,
        )


# ---------------------------------------------------------------------------
# Shared XML parsing helpers
# ---------------------------------------------------------------------------


def _check_xml_error(xml_text: str, indexer_name: str) -> None:
    """Check for and raise on Newznab XML error responses."""
    try:
        root = DefusedElementTree.fromstring(xml_text)
        error_el = root if root.tag == "error" else root.find("error")
        if error_el is not None:
            code = error_el.get("code", "?")
            description = error_el.get("description", "Unknown error")
            raise NewznabError(f"{indexer_name}: error {code} — {description}")
    except (ElementTree.ParseError, DefusedXmlException):
        pass


def _parse_capabilities(xml_text: str) -> IndexerCapabilities:
    """Parse a Newznab ?t=caps XML response."""
    try:
        root = DefusedElementTree.fromstring(xml_text)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise NewznabError(f"Failed to parse capabilities XML: {exc}") from None

    categories: list[str] = []
    for cat in root.iter("category"):
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        if cat_id and cat_name:
            categories.append(f"{cat_id}:{cat_name}")

    search_params: list[str] = []
    searching = root.find("searching")
    if searching is not None:
        for search_type in searching:
            if search_type.get("available") == "yes":
                search_params.append(search_type.tag)

    max_requests = None
    limits = root.find("limits")
    if limits is not None:
        max_requests = _safe_int(limits.get("max"))

    return IndexerCapabilities(
        categories=categories,
        search_params=search_params,
        max_requests_per_day=max_requests,
    )


def _parse_item_common(
    item: ElementTree.Element,
) -> tuple[str, str, int | None, dict[str, str], datetime | None, int | None, str | None]:
    """Extract fields shared by Newznab and Torznab item parsing.

    Returns (title, download_url, size_bytes, attrs, published_at, age_days, info_url).
    """
    title = item.findtext("title", "Unknown")

    enclosure = item.find("enclosure")
    if enclosure is not None:
        download_url = enclosure.get("url", "")
        size_bytes = _safe_int(enclosure.get("length"))
    else:
        download_url = item.findtext("link", "")
        size_bytes = None

    attrs = _parse_newznab_attrs(item)
    if size_bytes is None:
        size_bytes = _safe_int(attrs.get("size"))

    published_at = _parse_pub_date(item.findtext("pubDate"))
    age_days = _calc_age_days(published_at)

    # Info URL: prefer <comments>, fall back to <guid> if it looks like a URL
    info_url = item.findtext("comments")
    if not info_url:
        guid = item.findtext("guid", "")
        if guid.startswith("http"):
            info_url = guid

    return title, download_url, size_bytes, attrs, published_at, age_days, info_url


def _parse_newznab_attrs(item: ElementTree.Element) -> dict[str, str]:
    """Extract newznab:attr and torznab:attr name/value pairs from an RSS item.

    Multi-valued attributes (most commonly ``category``) are joined with
    commas so that all values are preserved.
    """
    attrs: dict[str, str] = {}
    for prefix in ("newznab:attr", "torznab:attr"):
        for attr in item.findall(prefix, _NS):
            name = attr.get("name", "")
            value = attr.get("value", "")
            if name and value:
                if name in attrs:
                    attrs[name] = f"{attrs[name]},{value}"
                else:
                    attrs[name] = value
    return attrs


def _parse_pub_date(pub_date: str | None) -> datetime | None:
    """Parse an RSS pubDate string to a datetime."""
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date)
    except (ValueError, TypeError):
        return None


def _calc_age_days(published_at: datetime | None) -> int | None:
    """Calculate age in days from a publication datetime."""
    if published_at is None:
        return None
    now = datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    delta = now - published_at
    return max(0, delta.days)


def _safe_int(value: Any) -> int | None:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
