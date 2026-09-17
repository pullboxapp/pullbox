"""Search retries must respect provider cooldowns across concurrent clients."""

import asyncio

import httpx
import pytest

from pullbox.core.provider_cooldown import retry_after_seconds
from pullbox.providers.indexer.newznab import NewznabError, NewznabIndexer


async def client(handler, *, url="https://indexer.test", key="account"):
    result = NewznabIndexer("Test", url, key, rate_limit_per_minute=6000)
    await result._client.aclose()
    result._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return result


@pytest.mark.parametrize("failure", ["429", "timeout", "xml500"])
async def test_failed_provider_is_not_hammered_by_next_client(failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "xml500":
            return httpx.Response(
                200,
                text='<error code="500" description="Request limit reached"/>',
                headers={"Retry-After": "900"},
            )
        return httpx.Response(429, headers={"Retry-After": "900"})

    first, second = await client(respond), await client(respond)
    try:
        with pytest.raises(NewznabError):
            await first._request({"t": "search"})
        with pytest.raises(NewznabError) as caught:
            await second._request({"t": "search"})
        assert len(requests) == 1, "A fresh client ignored the provider's cooldown"
        assert caught.value.retry_after_seconds > 0
    finally:
        await first.close()
        await second.close()


async def test_concurrent_clients_share_one_provider_request_slot():
    requests = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, text="<rss><channel/></rss>")

    first, second = await client(respond), await client(respond)
    tasks = []
    try:
        tasks.append(asyncio.create_task(first._request({"t": "search"})))
        await entered.wait()
        tasks.append(asyncio.create_task(second._request({"t": "search"})))
        await asyncio.sleep(0.02)
        assert len(requests) == 1, "Concurrent searches bypassed shared provider pacing"
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await first.close()
        await second.close()


async def test_throttled_indexer_does_not_block_another_provider():
    first = await client(lambda request: httpx.Response(429))
    second = await client(
        lambda request: httpx.Response(200, text="<rss/>"), url="https://other.test"
    )
    try:
        with pytest.raises(NewznabError):
            await first._request({"t": "search"})
        assert await second._request({"t": "search"}) == "<rss/>"
    finally:
        await first.close()
        await second.close()


async def test_expired_cooldown_allows_provider_recovery():
    responses = [httpx.Response(429), httpx.Response(200, text="<rss/>")]
    indexer = await client(lambda request: responses.pop(0))
    try:
        with pytest.raises(NewznabError):
            await indexer._request({"t": "search"})
        indexer._cooldown().until = 0
        assert await indexer._request({"t": "search"}) == "<rss/>"
    finally:
        await indexer.close()


@pytest.mark.parametrize("header", [None, "garbage", "-1", "nan", "inf"])
def test_malformed_retry_after_uses_safe_fallback(header):
    assert retry_after_seconds(header, default=60) == 60


def test_retry_after_accepts_seconds_and_http_dates():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    assert retry_after_seconds("120", default=900) == 120
    date = format_datetime(datetime.now(UTC) + timedelta(hours=2), usegmt=True)
    assert 7190 <= retry_after_seconds(date, default=60) <= 7200
