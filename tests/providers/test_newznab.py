"""Tests for the Newznab indexer provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import SearchQuery
from pullbox.providers.indexer.newznab import NewznabError, NewznabIndexer
from pullbox.providers.indexer.torznab import TorznabIndexer

_NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def _make_indexer(**kwargs: Any) -> NewznabIndexer:
    defaults: dict[str, Any] = {
        "name": "NZB Test",
        "url": "https://indexer.example",
        "api_key": "secret",
        "rate_limit_per_minute": 6000,
    }
    defaults.update(kwargs)
    return NewznabIndexer(**defaults)


def _make_response(
    *,
    text: str = "",
    status_code: int = 200,
) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


def _rss_with_item() -> str:
    return f"""\
<rss xmlns:newznab="{_NEWZNAB_NS}">
  <channel>
    <item>
      <title>Batman 001 (2026)</title>
      <link>https://indexer.example/link-fallback</link>
      <comments>https://indexer.example/details/123</comments>
      <guid>internal-guid</guid>
      <pubDate>Wed, 17 Jun 2026 12:00:00 GMT</pubDate>
      <enclosure url="https://indexer.example/download/123" length="123456"/>
      <newznab:attr name="grabs" value="7"/>
      <newznab:attr name="category" value="7030"/>
      <newznab:attr name="category" value="8010"/>
    </item>
  </channel>
</rss>
"""


def _caps_xml() -> str:
    return """\
<caps>
  <limits max="5000"/>
  <searching>
    <search available="yes"/>
    <tv-search available="no"/>
    <book-search available="yes"/>
  </searching>
  <categories>
    <category id="7030" name="Comics"/>
    <category id="8010" name="Magazines"/>
    <category id="" name="Ignored"/>
  </categories>
</caps>
"""


@pytest.mark.asyncio
class TestSearch:
    """Tests for Newznab search behavior."""

    async def test_search_formats_issue_categories_and_parses_releases(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(return_value=_rss_with_item())  # type: ignore[method-assign]

        results = await indexer.search(
            SearchQuery(
                series_title="Batman",
                issue_number=1.0,
                categories=["7030", "8010"],
            )
        )

        indexer._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            {"t": "search", "q": "Batman 1", "cat": "7030,8010"}
        )
        assert len(results) == 1
        result = results[0]
        assert result.title == "Batman 001 (2026)"
        assert result.indexer_name == "NZB Test"
        assert result.download_url == "https://indexer.example/download/123"
        assert result.size_bytes == 123456
        assert result.grabs == 7
        assert result.seeders is None
        assert result.leechers is None
        assert result.is_torrent is False
        assert result.protocol is AcquisitionProtocol.USENET
        assert result.category == "7030,8010"
        assert result.info_url == "https://indexer.example/details/123"
        assert result.published_at is not None

    async def test_torznab_results_use_torrent_protocol(self) -> None:
        indexer = TorznabIndexer(
            name="Torrent Test",
            url="https://indexer.example",
            api_key="secret",
            rate_limit_per_minute=6000,
        )
        indexer._request = AsyncMock(return_value=_rss_with_item())  # type: ignore[method-assign]

        results = await indexer.search(SearchQuery(series_title="Batman", issue_number=1.0))

        assert len(results) == 1
        assert results[0].protocol is AcquisitionProtocol.TORRENT
        assert results[0].is_torrent is True

    async def test_search_formats_decimal_issue_without_integer_coercion(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(return_value="<rss><channel /></rss>")  # type: ignore[method-assign]

        await indexer.search(SearchQuery(series_title="Annual Special", issue_number=1.5))

        indexer._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            {"t": "search", "q": "Annual Special 1.5"}
        )

    async def test_search_propagates_request_failure(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(side_effect=NewznabError("timeout"))  # type: ignore[method-assign]

        with pytest.raises(NewznabError, match="timeout"):
            await indexer.search(SearchQuery(series_title="Batman", issue_number=1.0))

    async def test_search_returns_empty_list_for_malformed_xml(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(return_value="<rss><channel>")  # type: ignore[method-assign]

        results = await indexer.search(SearchQuery(series_title="Batman"))

        assert results == []


@pytest.mark.asyncio
class TestRequest:
    """Tests for Newznab request failure mapping."""

    async def test_request_omits_empty_api_key(self) -> None:
        indexer = NewznabIndexer(
            name="Public Newznab",
            url="https://indexer.example",
            api_key="",
            rate_limit_per_minute=6000,
        )
        indexer._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(text="<caps><categories /></caps>")
        )

        await indexer._request({"t": "caps"})

        indexer._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "https://indexer.example/api",
            params={"t": "caps"},
        )

    async def test_request_includes_api_key_and_raises_on_root_error_response(self) -> None:
        indexer = _make_indexer()
        indexer._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(
                text='<error code="100" description="Invalid API key"/>',
            )
        )

        with pytest.raises(NewznabError, match="error 100"):
            await indexer._request({"t": "search", "q": "Batman"})

        indexer._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "https://indexer.example/api",
            params={"apikey": "secret", "t": "search", "q": "Batman"},
        )

    async def test_request_maps_http_status_errors(self) -> None:
        indexer = _make_indexer()
        response = _make_response(status_code=429)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "too many requests",
            request=MagicMock(),
            response=response,
        )
        indexer._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]

        with pytest.raises(NewznabError, match="HTTP 429"):
            await indexer._request({"t": "caps"})


@pytest.mark.asyncio
class TestCapabilitiesAndHealth:
    """Tests for Newznab capability and health behavior."""

    async def test_get_capabilities_parses_categories_search_types_and_limits(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(return_value=_caps_xml())  # type: ignore[method-assign]

        capabilities = await indexer.get_capabilities()

        assert capabilities.categories == ["7030:Comics", "8010:Magazines"]
        assert capabilities.search_params == ["search", "book-search"]
        assert capabilities.max_requests_per_day == 5000

    async def test_test_connection_reports_category_count(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(return_value=_caps_xml())  # type: ignore[method-assign]

        health = await indexer.test_connection()

        assert health.healthy is True
        assert health.message == "NZB Test: 2 categories available"
        assert health.details == {"categories": "2"}

    async def test_test_connection_reports_provider_error(self) -> None:
        indexer = _make_indexer()
        indexer._request = AsyncMock(side_effect=NewznabError("bad key"))  # type: ignore[method-assign]

        health = await indexer.test_connection()

        assert health.healthy is False
        assert health.message == "NZB Test error: bad key"
