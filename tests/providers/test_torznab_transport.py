"""Security and failover tests for manual Torznab browser resolution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest

import pullbox.providers.indexer.torznab_transport as torznab_transport_module
from pullbox.models.direct_acquisition import DirectResolverKind
from pullbox.providers.base import SearchQuery
from pullbox.providers.direct.resolver import (
    DirectResolverCookie,
    DirectResolverResult,
)
from pullbox.providers.indexer.torznab import TorznabIndexer
from pullbox.providers.indexer.torznab_transport import (
    TorznabDescriptor,
    TorznabTransport,
    TorznabTransportError,
)
from pullbox.services.direct_resolver_service import (
    NativeResolverOption,
    ResolverAttemptProgress,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


def _resolver(
    resolver_id: int,
    name: str,
    solve: Callable[[str, tuple[str, ...], str], Awaitable[DirectResolverResult]],
) -> NativeResolverOption:
    return NativeResolverOption(
        resolver_id=resolver_id,
        resolver_name=name,
        resolver_kind=DirectResolverKind.FLARESOLVERR,
        _solve=solve,
    )


def _solution(*, cookie: str = "clearance") -> DirectResolverResult:
    return DirectResolverResult(
        final_url="https://indexer.example/",
        status_code=200,
        html="<html>ready</html>",
        cookies=(DirectResolverCookie(name="cf_clearance", value=cookie),),
        user_agent="Pullbox resolver test",
    )


@pytest.mark.asyncio
async def test_challenge_uses_ranked_resolvers_without_disclosing_api_key() -> None:
    requests: list[httpx.Request] = []
    resolver_targets: list[str] = []
    progress: list[ResolverAttemptProgress] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("cookie") == "cf_clearance=second":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(
            503,
            text="<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>",
        )

    async def first(
        target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        resolver_targets.append(target)
        return _solution(cookie="first")

    async def second(
        target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        resolver_targets.append(target)
        return _solution(cookie="second")

    async def on_attempt(event: ResolverAttemptProgress) -> None:
        progress.append(event)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(
                _resolver(1, "FlareSolverr", first),
                _resolver(2, "Byparr", second),
            ),
            http_client=client,
        )
        text = await transport.get_text(
            "https://indexer.example/api",
            params={"apikey": "never-send-this", "t": "caps"},
            challenge_category="torznab_caps",
            on_attempt=on_attempt,
        )

    assert text == "<rss><channel /></rss>"
    assert resolver_targets == ["https://indexer.example/", "https://indexer.example/"]
    assert all("never-send-this" not in target for target in resolver_targets)
    assert [event.resolver_name for event in progress] == ["FlareSolverr", "Byparr"]
    assert [event.attempt for event in progress] == [1, 2]
    assert all(event.total == 2 for event in progress)
    assert all(request.url.params["apikey"] == "never-send-this" for request in requests)


@pytest.mark.asyncio
async def test_plain_forbidden_response_does_not_invoke_resolver() -> None:
    solve = AsyncMock(return_value=_solution())

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Access denied")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(_resolver(1, "FlareSolverr", solve),),
            http_client=client,
        )
        with pytest.raises(TorznabTransportError, match="HTTP 403"):
            await transport.get_text(
                "https://indexer.example/api",
                params={"apikey": "secret", "t": "caps"},
                challenge_category="torznab_caps",
            )

    solve.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_status_challenge_page_still_uses_resolver() -> None:
    solve = AsyncMock(return_value=_solution())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "cf_clearance=clearance":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(
            200,
            text="<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(_resolver(1, "FlareSolverr", solve),),
            http_client=client,
        )
        text = await transport.get_text(
            "https://indexer.example/api",
            params={"apikey": "secret", "t": "caps"},
            challenge_category="torznab_caps",
        )

    assert text == "<rss><channel /></rss>"
    solve.assert_awaited_once()


@pytest.mark.asyncio
async def test_cached_clearance_is_reused_without_another_solve() -> None:
    solve_calls = 0

    async def solve(
        _target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        nonlocal solve_calls
        solve_calls += 1
        return _solution()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "cf_clearance=clearance":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(503, text="Just a moment... cf-chl-widget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(_resolver(1, "FlareSolverr", solve),),
            http_client=client,
            cache_namespace="cache-reuse-test",
        )
        for _ in range(2):
            await transport.get_text(
                "https://indexer.example/api",
                params={"apikey": "secret", "t": "caps"},
                challenge_category="torznab_caps",
            )

    assert solve_calls == 1


@pytest.mark.asyncio
async def test_concurrent_challenges_share_one_resolver_solve() -> None:
    solve_started = asyncio.Event()
    release_solve = asyncio.Event()
    solve_calls = 0

    async def solve(
        _target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        nonlocal solve_calls
        solve_calls += 1
        solve_started.set()
        await release_solve.wait()
        return _solution()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "cf_clearance=clearance":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(503, text="Just a moment... cf-chl-widget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(_resolver(1, "FlareSolverr", solve),),
            http_client=client,
            cache_namespace="singleflight-test",
        )

        async def request() -> str:
            return await transport.get_text(
                "https://indexer.example/api",
                params={"apikey": "secret", "t": "caps"},
                challenge_category="torznab_caps",
            )

        first = asyncio.create_task(request())
        await solve_started.wait()
        second = asyncio.create_task(request())
        release_solve.set()
        assert await asyncio.gather(first, second) == [
            "<rss><channel /></rss>",
            "<rss><channel /></rss>",
        ]

    assert solve_calls == 1


@pytest.mark.asyncio
async def test_descriptor_fetch_reuses_resolver_and_returns_torrent_bytes() -> None:
    async def solve(
        _target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        return _solution()

    torrent = b"d8:announce15:https://tracker4:infod4:name4:testee"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "cf_clearance=clearance":
            return httpx.Response(
                200,
                content=torrent,
                headers={"content-type": "application/x-bittorrent"},
            )
        return httpx.Response(503, text="Just a moment... cf-chl-widget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            resolver_options=(_resolver(1, "FlareSolverr", solve),),
            http_client=client,
            cache_namespace="descriptor-test",
        )
        descriptor = await transport.fetch_descriptor(
            "https://indexer.example/api?t=get&id=42&apikey=secret",
        )

    assert descriptor == TorznabDescriptor(content=torrent, magnet_url=None)


@pytest.mark.asyncio
async def test_descriptor_accepts_magnet_redirect_but_rejects_html() -> None:
    responses = [
        httpx.Response(302, headers={"location": "magnet:?xt=urn:btih:abc"}),
        httpx.Response(200, text="<html>not a torrent</html>"),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(http_client=client)
        descriptor = await transport.fetch_descriptor(
            "https://indexer.example/api?t=get&id=42&apikey=secret"
        )
        assert descriptor.magnet_url == "magnet:?xt=urn:btih:abc"

        with pytest.raises(TorznabTransportError, match="torrent descriptor"):
            await transport.fetch_descriptor(
                "https://indexer.example/api?t=get&id=43&apikey=secret"
            )


@pytest.mark.asyncio
async def test_descriptor_follows_four_same_origin_redirects() -> None:
    torrent = b"d8:announce15:https://tracker4:infod4:name4:testee"
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count <= 4:
            return httpx.Response(302, headers={"location": f"/descriptor/{request_count}"})
        return httpx.Response(200, content=torrent)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            http_client=client,
            configured_base_url="https://indexer.example",
        )
        descriptor = await transport.fetch_descriptor(
            "https://indexer.example/api?t=get&id=42&apikey=secret"
        )

    assert descriptor == TorznabDescriptor(content=torrent, magnet_url=None)
    assert request_count == 5


@pytest.mark.asyncio
async def test_descriptor_returns_direct_magnet_without_network_request() -> None:
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500)

    magnet = "magnet:?xt=urn:btih:abc"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        descriptor = await TorznabTransport(http_client=client).fetch_descriptor(magnet)

    assert descriptor == TorznabDescriptor(content=None, magnet_url=magnet)
    assert request_count == 0


@pytest.mark.asyncio
async def test_descriptor_rejects_url_outside_configured_indexer_origin() -> None:
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, content=b"d4:info0:e")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            http_client=client,
            configured_base_url="https://indexer.example",
        )
        with pytest.raises(TorznabTransportError, match="configured Torznab origin"):
            await transport.fetch_descriptor("https://attacker.example/file.torrent")

    assert request_count == 0


@pytest.mark.parametrize("content", [b"d4:info11:not-an-infoe", b"d4:infod4:name99:truncatedee"])
async def test_descriptor_rejects_malformed_bencode_before_handoff(content: bytes) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    ) as client:
        transport = TorznabTransport(
            http_client=client, configured_base_url="https://indexer.example"
        )
        with pytest.raises(TorznabTransportError, match="valid torrent descriptor"):
            await transport.fetch_descriptor("https://indexer.example/get")


async def test_descriptor_redirect_policy_overrides_client_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "indexer.example":
            return httpx.Response(302, headers={"location": "http://unconfigured.example/private"})
        return httpx.Response(200, content=b"d4:infod4:name4:testee")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        transport = TorznabTransport(
            http_client=client, configured_base_url="https://indexer.example"
        )
        with pytest.raises(TorznabTransportError, match="outside its configured origin"):
            await transport.fetch_descriptor("https://indexer.example/get")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_descriptor_stream_stops_at_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.yield_count = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"d4:inf", b"o0:eXX", b"never-read"):
                self.yield_count += 1
                yield chunk

    monkeypatch.setattr(torznab_transport_module, "_MAX_DESCRIPTOR_BYTES", 10)
    stream = CountingStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = TorznabTransport(
            http_client=client,
            configured_base_url="https://indexer.example",
        )
        with pytest.raises(TorznabTransportError, match="safe size limit"):
            await transport.fetch_descriptor("https://indexer.example/file.torrent")

    assert stream.yield_count == 2


@pytest.mark.asyncio
async def test_singleflight_key_cap_fails_fast_when_all_slots_are_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torznab_transport_module, "_MAX_CACHE_ENTRIES", 1)
    torznab_transport_module._solve_locks.clear()
    torznab_transport_module._session_cache.clear()
    solve_started = asyncio.Event()
    release_solve = asyncio.Event()
    solve_calls = 0

    async def solve(
        _target: str,
        _domains: tuple[str, ...],
        _category: str,
    ) -> DirectResolverResult:
        nonlocal solve_calls
        solve_calls += 1
        if solve_calls > 1:
            return _solution()
        solve_started.set()
        await release_solve.wait()
        return _solution()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "cf_clearance=clearance":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(503, text="Just a moment... cf-chl-widget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = _resolver(1, "FlareSolverr", solve)
        first_transport = TorznabTransport(
            resolver_options=(resolver,),
            http_client=client,
            cache_namespace="singleflight-cap-first",
        )
        second_transport = TorznabTransport(
            resolver_options=(resolver,),
            http_client=client,
            cache_namespace="singleflight-cap-second",
        )
        first = asyncio.create_task(
            first_transport.get_text(
                "https://one.example/api",
                params={"apikey": "secret", "t": "caps"},
                challenge_category="torznab_caps",
            )
        )
        await solve_started.wait()
        try:
            with pytest.raises(TorznabTransportError, match="too many browser challenges"):
                await second_transport.get_text(
                    "https://two.example/api",
                    params={"apikey": "secret", "t": "caps"},
                    challenge_category="torznab_caps",
                )
        finally:
            release_solve.set()
            await first
            torznab_transport_module._solve_locks.clear()
            torznab_transport_module._session_cache.clear()


@pytest.mark.asyncio
async def test_torznab_indexer_routes_caps_search_and_descriptor_through_transport() -> None:
    transport = AsyncMock()
    transport.get_text.side_effect = [
        "<caps><categories /></caps>",
        "<rss><channel /></rss>",
    ]
    expected_descriptor = TorznabDescriptor(content=b"d4:info0:e", magnet_url=None)
    transport.fetch_descriptor.return_value = expected_descriptor
    indexer = TorznabIndexer(
        name="Manual Torznab",
        url="https://indexer.example",
        api_key="secret-key",
        rate_limit_per_minute=6000,
        request_transport=transport,
    )

    await indexer.get_capabilities()
    await indexer.search(SearchQuery(series_title="Ubuntu"))
    descriptor = await indexer.fetch_torrent_descriptor(
        "https://indexer.example/api?t=get&id=42&apikey=secret-key"
    )

    assert descriptor is expected_descriptor
    assert transport.get_text.await_args_list[0].kwargs["params"] == {
        "apikey": "secret-key",
        "t": "caps",
    }
    assert transport.get_text.await_args_list[1].kwargs["params"] == {
        "apikey": "secret-key",
        "t": "search",
        "q": "Ubuntu",
    }
    transport.fetch_descriptor.assert_awaited_once()


@pytest.mark.asyncio
async def test_torznab_indexer_omits_empty_api_key() -> None:
    transport = AsyncMock()
    transport.get_text.return_value = "<caps><categories /></caps>"
    indexer = TorznabIndexer(
        name="Public Torznab",
        url="https://indexer.example",
        api_key="",
        rate_limit_per_minute=6000,
        request_transport=transport,
    )

    await indexer.get_capabilities()

    assert transport.get_text.await_args.kwargs["params"] == {"t": "caps"}


def test_opted_in_torznab_keeps_descriptor_handoff_without_healthy_resolvers() -> None:
    indexer = TorznabIndexer(
        name="Manual Torznab",
        url="https://indexer.example",
        api_key="secret-key",
        resolver_enabled=True,
        resolver_options=(),
    )

    assert indexer.browser_resolver_enabled is True
