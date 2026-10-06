"""GCD's documented token wire contract, without real credentials or traffic."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.providers.metadata.gcd_api_v2 import exchange_gcd_token
from pullbox.schemas.metadata_sources import SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError

USERNAME = "synthetic-gcd-account"
PASSWORD = "synthetic private password \u00e9 +&"
TOKEN = "synthetic-exchanged-gcd-token"


async def exchange(handler, cooldown=None):
    return await exchange_gcd_token(
        SecretStr(USERNAME),
        SecretStr(PASSWORD),
        transport=httpx.MockTransport(handler),
        cooldown=cooldown or ProviderCooldown(),
    )


async def test_exchange_sends_exact_credentials_only_in_fixed_https_json_body():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url == "https://beta.comics.org/api/v2/auth/token/"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["content-type"] == "application/json"
        assert "Pullbox/" in request.headers["user-agent"]
        assert json.loads(request.content) == {"username": USERNAME, "password": PASSWORD}
        return httpx.Response(200, json={"token": TOKEN})

    result = await exchange(handler)
    assert isinstance(result, SecretStr) and result.get_secret_value() == TOKEN
    assert TOKEN not in repr(result) and len(requests) == 1


@pytest.mark.parametrize(
    "code,status",
    [
        (400, SourceStatus.AUTHENTICATION_FAILED),
        (401, SourceStatus.AUTHENTICATION_FAILED),
        (403, SourceStatus.AUTHENTICATION_FAILED),
        (429, SourceStatus.RATE_LIMITED),
        (503, SourceStatus.UNAVAILABLE),
        (302, SourceStatus.INCOMPATIBLE_RESPONSE),
    ],
)
async def test_exchange_failures_are_safe_and_do_not_retry_or_follow_redirects(code, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            code,
            text=f"{USERNAME} {PASSWORD} {TOKEN}",
            headers={
                "Location": "https://foreign.example.test/token/",
                "Retry-After": "1.5",
            },
        )

    with pytest.raises(MetadataSourceError) as raised:
        await exchange(handler)
    assert raised.value.status is status
    assert all(secret not in str(raised.value) for secret in (USERNAME, PASSWORD, TOKEN))
    assert len(requests) == 1
    if code in {429, 503}:
        assert raised.value.retry_after_seconds == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": ""},
        {"token": True},
        {"token": "space token"},
        {"token": "enc:not-a-plain-token"},
        {"token": "\u00e9"},
        {"token": "a" * 4097},
        {"token": TOKEN, "password": PASSWORD},
    ],
)
async def test_exchange_refuses_invalid_or_unexpected_token_payload(payload):
    with pytest.raises(MetadataSourceError) as raised:
        await exchange(lambda _: httpx.Response(200, json=payload))
    assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE


@pytest.mark.parametrize(
    "content",
    [
        b'{"token":"one","token":"two"}',
        b'{"token":NaN}',
        b'{"token":',
        b"[1,2]",
        b"a" * 8193,
    ],
)
async def test_exchange_rejects_malformed_duplicate_and_oversized_json(content):
    with pytest.raises(MetadataSourceError) as raised:
        await exchange(
            lambda _: httpx.Response(
                200,
                content=content,
                headers={
                    "content-type": "application/json",
                },
            )
        )
    assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE


async def test_exchange_honors_shared_hold_before_sending_another_password():
    cooldown, requests = ProviderCooldown(), []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    for _ in range(2):
        with pytest.raises(MetadataSourceError) as raised:
            await exchange(handler, cooldown)
        assert raised.value.status is SourceStatus.RATE_LIMITED
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure,status",
    [
        (httpx.ReadTimeout(PASSWORD), SourceStatus.TIMEOUT),
        (httpx.ConnectError(PASSWORD), SourceStatus.UNAVAILABLE),
    ],
)
async def test_exchange_does_not_expose_transport_exception_secrets(failure, status):
    def handler(_):
        raise failure

    with pytest.raises(MetadataSourceError) as raised:
        await exchange(handler)
    assert raised.value.status is status and PASSWORD not in str(raised.value)


async def test_exchange_propagates_cancellation_instead_of_claiming_a_saved_token():
    async def handler(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await exchange(handler)
