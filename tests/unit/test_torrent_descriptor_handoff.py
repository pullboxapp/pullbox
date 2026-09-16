"""Torrent metadata stays in Pullbox even when the client cannot reach an indexer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from pullbox.core.events import EventBus
from pullbox.core.exceptions import ProviderError
from pullbox.providers.base import ProviderRegistry
from pullbox.providers.indexer.newznab import NewznabError
from pullbox.providers.indexer.prowlarr import ProwlarrError, ProwlarrIndexer
from pullbox.providers.indexer.torznab import TorznabIndexer
from pullbox.providers.indexer.torznab_transport import TorznabDescriptor
from pullbox.services.download_service import DownloadService
from pullbox.tasks.download_progress import clear_download_progress


@pytest.mark.parametrize("resolver_enabled", [False, True])
@pytest.mark.parametrize("result", ["bytes", "magnet", "failure"])
async def test_http_handoff_fetches_before_submitting(resolver_enabled: bool, result: str) -> None:
    registry = ProviderRegistry()
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    indexer = SimpleNamespace(
        browser_resolver_enabled=resolver_enabled,
        fetch_torrent_descriptor=AsyncMock(
            return_value=TorznabDescriptor(
                content=b"fixture" if result == "bytes" else None,
                magnet_url=magnet if result == "magnet" else None,
            )
        ),
    )
    registry.register_indexer(6, indexer)
    client = SimpleNamespace(
        add_torrent=AsyncMock(return_value="hash"), add_torrent_data=AsyncMock(return_value="hash")
    )
    service = DownloadService(registry, EventBus())
    if result == "failure":
        indexer.fetch_torrent_descriptor.side_effect = NewznabError("Descriptor unavailable")
    try:
        if result == "failure":
            with pytest.raises(NewznabError, match="Descriptor unavailable"):
                await service.add_torrent_to_client(
                    client,
                    url="https://indexer.example/get",
                    title="Fixture",
                    indexer_id=6,
                    download_id=123,
                )
            client.add_torrent.assert_not_awaited()
            client.add_torrent_data.assert_not_awaited()
        else:
            assert (
                await service.add_torrent_to_client(
                    client,
                    url="https://indexer.example/get",
                    title="Fixture",
                    indexer_id=6,
                    download_id=123,
                )
                == "hash"
            )
            indexer.fetch_torrent_descriptor.assert_awaited_once()
            if result == "bytes":
                client.add_torrent_data.assert_awaited_once_with(b"fixture", "Fixture")
                client.add_torrent.assert_not_awaited()
            else:
                client.add_torrent.assert_awaited_once_with(magnet, "Fixture")
                client.add_torrent_data.assert_not_awaited()
    finally:
        clear_download_progress(123)


@pytest.mark.parametrize(
    "url", ["https://unconfigured.example/get", "file:///tmp/file", "ftp://host/file"]
)
async def test_http_handoff_without_source_never_forwards_url(url: str) -> None:
    client = SimpleNamespace(add_torrent=AsyncMock(), add_torrent_data=AsyncMock())
    service = DownloadService(ProviderRegistry(), EventBus())
    with pytest.raises(ProviderError):
        await service.add_torrent_to_client(
            client, url=url, title="Fixture", indexer_id=None, download_id=123
        )
    client.add_torrent.assert_not_awaited()
    client.add_torrent_data.assert_not_awaited()


async def test_magnet_handoff_does_not_fetch_http() -> None:
    client = SimpleNamespace(
        add_torrent=AsyncMock(return_value="hash"), add_torrent_data=AsyncMock()
    )
    service = DownloadService(ProviderRegistry(), EventBus())
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    assert (
        await service.add_torrent_to_client(
            client, url=magnet, title="Fixture", indexer_id=None, download_id=123
        )
        == "hash"
    )
    client.add_torrent.assert_awaited_once_with(magnet, "Fixture")
    client.add_torrent_data.assert_not_awaited()


@pytest.mark.parametrize("provider", ["prowlarr", "torznab"])
@pytest.mark.parametrize(
    "response_kind",
    ["torrent", "magnet", "redirect", "html", "http_error", "timeout", "cross_origin", "oversize"],
)
async def test_configured_provider_descriptor_transport(provider: str, response_kind: str) -> None:
    content = (
        b"d4:infod6:lengthi1e4:name4:test12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"
    )
    magnet = "magnet:?xt=urn:btih:" + "b" * 40
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if response_kind == "magnet":
            return httpx.Response(302, headers={"location": magnet})
        if response_kind == "redirect" and request.url.path != "/final":
            return httpx.Response(302, headers={"location": "/final"})
        if response_kind == "cross_origin":
            return httpx.Response(302, headers={"location": "http://unconfigured.example/secret"})
        if response_kind == "html":
            return httpx.Response(200, text="<html>Login required</html>")
        if response_kind == "http_error":
            return httpx.Response(401)
        if response_kind == "timeout":
            raise httpx.ReadTimeout("https://indexer.example/get?apikey=fixture-secret")
        if response_kind == "oversize":
            return httpx.Response(200, headers={"content-length": str(20 * 1024 * 1024)})
        return httpx.Response(200, content=content)

    if provider == "prowlarr":
        indexer = ProwlarrIndexer(url="https://indexer.example", api_key="fixture-secret")
    else:
        indexer = TorznabIndexer(
            name="Torznab",
            url="https://indexer.example",
            api_key="fixture-secret",
            resolver_enabled=False,
        )
    registry = ProviderRegistry()
    registry.register_indexer(6, indexer)
    client = SimpleNamespace(
        add_torrent=AsyncMock(return_value="hash"), add_torrent_data=AsyncMock(return_value="hash")
    )
    service = DownloadService(registry, EventBus())
    await indexer._client.aclose()
    indexer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    if provider == "torznab":
        indexer._request_transport._client = indexer._client
    try:
        if response_kind in {"torrent", "magnet", "redirect"}:
            assert (
                await service.add_torrent_to_client(
                    client,
                    url="https://indexer.example/get?apikey=fixture-secret",
                    title="Fixture",
                    indexer_id=6,
                    download_id=123,
                )
                == "hash"
            )
            assert requests
            if response_kind == "magnet":
                client.add_torrent.assert_awaited_once_with(magnet, "Fixture")
            else:
                client.add_torrent_data.assert_awaited_once_with(content, "Fixture")
                client.add_torrent.assert_not_awaited()
        else:
            with pytest.raises((ProwlarrError, NewznabError, ProviderError)) as error:
                await service.add_torrent_to_client(
                    client,
                    url="https://indexer.example/get?apikey=fixture-secret",
                    title="Fixture",
                    indexer_id=6,
                    download_id=123,
                )
            assert "fixture-secret" not in str(error.value)
            client.add_torrent.assert_not_awaited()
            client.add_torrent_data.assert_not_awaited()
        assert all(request.url.host == "indexer.example" for request in requests)
    finally:
        await indexer.close()
        clear_download_progress(123)


async def test_seedbox_receives_multipart_torrent_not_private_prowlarr_url() -> None:
    from pullbox.core.torrent_metadata import torrent_info_hashes
    from pullbox.providers.download.qbittorrent import QBittorrentClient

    content = (
        b"d4:infod6:lengthi1e4:name4:test12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"
    )
    info_hash = next(value for value in torrent_info_hashes(content) if len(value) == 40)
    fetched: list[httpx.Request] = []
    uploaded: list[bytes] = []

    def source_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "prowlarr.internal"
        assert request.url.params["apikey"] == "private-indexer-key"
        assert "cookie" not in request.headers
        fetched.append(request)
        return httpx.Response(200, content=content)

    async def seedbox_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "seedbox.example"
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=fixture-session"})
        if request.url.path.endswith("/torrents/add"):
            assert request.headers["content-type"].startswith("multipart/form-data;")
            body = await request.aread()
            assert b'name="torrents"' in body
            assert content in body
            assert b'name="urls"' not in body
            assert b"prowlarr.internal" not in body
            assert b"private-indexer-key" not in body
            uploaded.append(body)
            return httpx.Response(200, text="Ok.")
        assert request.url.path.endswith("/torrents/info")
        return httpx.Response(200, json=[{"hash": info_hash}] if uploaded else [])

    indexer = ProwlarrIndexer(url="https://prowlarr.internal", api_key="private-indexer-key")
    client = QBittorrentClient(
        url="https://seedbox.example", username="fixture", password="fixture"
    )
    await indexer._client.aclose()
    await client._client.aclose()
    indexer._client = httpx.AsyncClient(transport=httpx.MockTransport(source_handler), timeout=5)
    client._client = httpx.AsyncClient(
        base_url="https://seedbox.example/",
        transport=httpx.MockTransport(seedbox_handler),
        timeout=5,
    )
    registry = ProviderRegistry()
    registry.register_indexer(-1, indexer)
    registry.register_indexer_alias(6, -1)
    try:
        result = await DownloadService(registry, EventBus()).add_torrent_to_client(
            client,
            url="https://prowlarr.internal/4/download?apikey=private-indexer-key",
            title="Synthetic fixture",
            indexer_id=6,
            download_id=123,
        )
        assert result == info_hash
        assert len(fetched) == 1
        assert len(uploaded) == 1
    finally:
        await indexer.close()
        await client.close()
        clear_download_progress(123)
