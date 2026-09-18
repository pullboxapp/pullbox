"""Provider throttling must apply across ComicVine clients and resource types."""

import httpx
import pytest

from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider


@pytest.mark.parametrize("status,body", [(420, {}), (429, {}), (200, {"status_code": 107})])
async def test_throttle_stops_other_clients_and_other_resources(status, body):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Retry-After": "7200"})

    first = ComicVineProvider("same-account")
    second = ComicVineProvider("same-account", rate_limit=100)
    await first._client.aclose()
    await second._client.aclose()
    first._client = httpx.AsyncClient(
        base_url="https://comicvine.test", transport=httpx.MockTransport(respond)
    )
    second._client = httpx.AsyncClient(
        base_url="https://comicvine.test", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(ComicVineError):
            await first._request("/issues/")
        with pytest.raises(ComicVineError) as caught:
            await second._request("/volume/4050-1/")
        assert len(requests) == 1, "A second client ignored the account's throttle"
        assert caught.value.retryable
        assert caught.value.retry_after_seconds >= 7190
    finally:
        await first.close()
        await second.close()
