"""Tests for the Prowlarr indexer provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import SearchQuery
from pullbox.providers.indexer.prowlarr import ProwlarrError, ProwlarrIndexer


def _make_indexer(**kwargs: Any) -> ProwlarrIndexer:
    defaults: dict[str, Any] = {
        "url": "http://prowlarr:9696",
        "api_key": "secret",
    }
    defaults.update(kwargs)
    return ProwlarrIndexer(**defaults)


def _make_response(
    *,
    json_data: Any = None,
    text: str = "",
    status_code: int = 200,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = [] if json_data is None else json_data
    response.raise_for_status = MagicMock()
    return response


def _search_rows() -> list[dict[str, Any]]:
    return [
        {
            "title": "Batman 001 (2026)",
            "indexer": "NZBgeek",
            "protocol": "usenet",
            "downloadUrl": "https://prowlarr.example/download/nzb",
            "size": 123456,
            "publishDate": "2026-06-17T12:00:00+00:00",
            "grabs": 9,
            "infoUrl": "https://nzbgeek.example/details/123",
            "categories": [
                {"id": 7030, "name": "Books/Comics"},
                {"id": 8010, "name": "Magazines"},
            ],
        },
        {
            "title": "Batman 002 (2026)",
            "indexer": "TorrentGalaxy",
            "protocol": "torrent",
            "guid": "https://torrent.example/details/456",
            "size": 654321,
            "publishDate": "not-a-date",
            "seeders": 42,
            "leechers": 3,
            "categories": [{"id": 7030, "name": "Books/Comics"}],
        },
    ]


@pytest.mark.asyncio
class TestRequestPlumbing:
    """Tests for Prowlarr HTTP request helpers."""

    async def test_api_request_sends_api_key_header_params_and_timeout(self) -> None:
        indexer = _make_indexer()
        indexer._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(json_data=[{"id": 1}])
        )

        result = await indexer._api_request(
            "/search",
            {"query": "Batman"},
            timeout=45.0,
        )

        assert result == [{"id": 1}]
        indexer._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "http://prowlarr:9696/api/v1/search",
            headers={"X-Api-Key": "secret"},
            params={"query": "Batman"},
            timeout=45.0,
        )

    async def test_api_request_maps_timeout_to_provider_error(self) -> None:
        indexer = _make_indexer()
        indexer._client.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))  # type: ignore[method-assign]

        with pytest.raises(ProwlarrError, match="Request timed out: /indexer"):
            await indexer._api_request("/indexer")

    async def test_api_request_maps_http_status_to_provider_error(self) -> None:
        indexer = _make_indexer()
        response = _make_response(status_code=401)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "unauthorized",
            request=MagicMock(),
            response=response,
        )
        indexer._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]

        with pytest.raises(ProwlarrError, match="HTTP 401: /indexer"):
            await indexer._api_request("/indexer")

    async def test_newznab_request_returns_raw_xml(self) -> None:
        indexer = _make_indexer()
        indexer._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(text="<rss />")
        )

        result = await indexer._newznab_request({"t": "caps"})

        assert result == "<rss />"
        indexer._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "http://prowlarr:9696/api/v1/indexer/1/newznab",
            params={"apikey": "secret", "t": "caps"},
        )


@pytest.mark.asyncio
class TestSearch:
    """Tests for Prowlarr aggregate search behavior."""

    async def test_search_formats_params_and_maps_usenet_and_torrent_results(self) -> None:
        indexer = _make_indexer(indexer_ids=[1, 2])
        indexer._api_request = AsyncMock(return_value=_search_rows())  # type: ignore[method-assign]

        results = await indexer.search(
            SearchQuery(
                series_title="Batman",
                issue_number=1.0,
                categories=["7030", "8010"],
            )
        )

        indexer._api_request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "/search",
            {
                "query": "Batman 1",
                "type": "search",
                "categories": [7030, 8010],
                "indexerIds": [1, 2],
            },
            timeout=45.0,
        )
        assert len(results) == 2

        nzb = results[0]
        assert nzb.title == "Batman 001 (2026)"
        assert nzb.indexer_name == "NZBgeek"
        assert nzb.download_url == "https://prowlarr.example/download/nzb"
        assert nzb.size_bytes == 123456
        assert nzb.grabs == 9
        assert nzb.seeders is None
        assert nzb.leechers is None
        assert nzb.is_torrent is False
        assert nzb.protocol is AcquisitionProtocol.USENET
        assert nzb.category == "Books/Comics,Magazines"
        assert nzb.info_url == "https://nzbgeek.example/details/123"
        assert nzb.published_at is not None

        torrent = results[1]
        assert torrent.indexer_name == "TorrentGalaxy"
        assert torrent.download_url == "https://torrent.example/details/456"
        assert torrent.seeders == 42
        assert torrent.leechers == 3
        assert torrent.grabs is None
        assert torrent.is_torrent is True
        assert torrent.protocol is AcquisitionProtocol.TORRENT
        assert torrent.info_url == "https://torrent.example/details/456"
        assert torrent.published_at is None

    async def test_search_formats_decimal_issue_without_integer_coercion(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(return_value=[])  # type: ignore[method-assign]

        await indexer.search(SearchQuery(series_title="Annual Special", issue_number=1.5))

        indexer._api_request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "/search",
            {"query": "Annual Special 1.5", "type": "search"},
            timeout=45.0,
        )

    async def test_search_propagates_prowlarr_error(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(side_effect=ProwlarrError("down"))  # type: ignore[method-assign]

        with pytest.raises(ProwlarrError, match="down"):
            await indexer.search(SearchQuery(series_title="Batman"))


@pytest.mark.asyncio
class TestCapabilitiesAndHealth:
    """Tests for Prowlarr capabilities, health, and lifecycle."""

    async def test_get_capabilities_deduplicates_categories(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "capabilities": {
                        "categories": [
                            {"id": 7030, "name": "Comics"},
                            {"id": 8010, "name": "Magazines"},
                        ]
                    }
                },
                {
                    "capabilities": {
                        "categories": [
                            {"id": 7030, "name": "Comics"},
                            {"id": "", "name": "Ignored"},
                        ]
                    }
                },
            ]
        )

        capabilities = await indexer.get_capabilities()

        assert capabilities.categories == ["7030:Comics", "8010:Magazines"]
        assert capabilities.search_params == ["search"]

    async def test_get_capabilities_returns_empty_when_prowlarr_errors(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(side_effect=ProwlarrError("down"))  # type: ignore[method-assign]

        capabilities = await indexer.get_capabilities()

        assert capabilities.categories == []
        assert capabilities.search_params == []

    async def test_test_connection_reports_indexer_count(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(return_value=[{"id": 1}, {"id": 2}])  # type: ignore[method-assign]

        health = await indexer.test_connection()

        assert health.healthy is True
        assert health.message == "Prowlarr: 2 indexer(s) configured"
        assert health.details == {"indexer_count": "2"}

    async def test_test_connection_reports_provider_error(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(side_effect=ProwlarrError("bad key"))  # type: ignore[method-assign]

        health = await indexer.test_connection()

        assert health.healthy is False
        assert health.message == "Prowlarr error: bad key"

    async def test_test_connection_reports_unexpected_error(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        health = await indexer.test_connection()

        assert health.healthy is False
        assert health.message == "Connection failed: boom"

    async def test_get_indexers_delegates_to_indexer_endpoint(self) -> None:
        indexer = _make_indexer()
        indexer._api_request = AsyncMock(return_value=[{"id": 1, "name": "NZBgeek"}])  # type: ignore[method-assign]

        result = await indexer.get_indexers()

        assert result == [{"id": 1, "name": "NZBgeek"}]
        indexer._api_request.assert_awaited_once_with("/indexer")  # type: ignore[attr-defined]

    async def test_close_closes_rest_and_xml_parser_clients(self) -> None:
        indexer = _make_indexer()
        indexer._client.aclose = AsyncMock()  # type: ignore[method-assign]
        indexer._xml_parser.close = AsyncMock()  # type: ignore[method-assign]

        await indexer.close()

        indexer._client.aclose.assert_awaited_once()  # type: ignore[attr-defined]
        indexer._xml_parser.close.assert_awaited_once()  # type: ignore[attr-defined]
