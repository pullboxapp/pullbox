"""Bounded AirDC++ REST client tests using deterministic HTTP fakes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from pullbox.providers.airdcpp.api_client import AirDcppApiClient
from pullbox.providers.airdcpp.errors import (
    AirDcppAuthenticationError,
    AirDcppConflictError,
    AirDcppEntityNotFoundError,
    AirDcppPermissionError,
    AirDcppRateLimitError,
    AirDcppResponseError,
    AirDcppUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _system_info() -> dict[str, object]:
    return {
        "api_version": 1,
        "api_feature_level": 10,
        "client_version": "AirDC++w 2.14.0 x86_64",
        "platform": "linux",
        "path_separator": "/",
    }


def _user() -> dict[str, object]:
    return {
        "username": "pullbox",
        "permissions": [
            "search",
            "download",
            "queue_view",
            "queue_edit",
            "hubs_view",
            "settings_view",
        ],
    }


def _auth() -> dict[str, object]:
    return {
        "session_id": 123,
        "auth_token": "server-bearer-token",
        "token_type": "Bearer",
        "system_info": _system_info(),
        "user": _user(),
        "wizard_pending": False,
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_response_bytes: int = 1_048_576,
) -> AirDcppApiClient:
    return AirDcppApiClient(
        base_url="http://airdcpp.test:5600",
        username="pullbox",
        password="local-password",
        timeout_seconds=15,
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
    )


async def test_authorize_uses_exact_path_and_bearer_for_read_methods() -> None:
    requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/v1/sessions/authorize":
            assert json.loads(request.content) == {
                "username": "pullbox",
                "password": "local-password",
            }
            return httpx.Response(200, json={**_auth(), "additive": True})
        if request.url.path == "/api/v1/sessions/self" and request.method == "GET":
            return httpx.Response(200, json={"id": 123, "user": _user()})
        if request.url.path == "/api/v1/system/system_info":
            return httpx.Response(200, json=_system_info())
        if request.url.path == "/api/v1/hubs":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v1/settings/get":
            assert json.loads(request.content) == {"keys": ["min_search_interval"]}
            return httpx.Response(200, json={"min_search_interval": 45})
        if request.url.path == "/api/v1/queue/bundles/0/1":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v1/sessions/self" and request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    client = _client(handler)
    auth = await client.authorize()
    session = await client.get_current_session()
    system_info = await client.get_system_info()
    hubs = await client.get_hubs()
    settings = await client.get_settings(["min_search_interval"])
    bundles = await client.get_queue_bundles(start=0, count=1)
    await client.delete_current_session()
    await client.aclose()

    assert auth.session_id == 123
    assert session.user.username == "pullbox"
    assert system_info.api_version == 1
    assert hubs == []
    assert settings == [45]
    assert bundles == []
    assert requests[0] == ("POST", "/api/v1/sessions/authorize", None)
    assert all(auth_header == "Bearer server-bearer-token" for _, _, auth_header in requests[1:])


async def test_get_settings_normalizes_keyed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sessions/authorize":
            return httpx.Response(200, json=_auth())
        assert request.url.path == "/api/v1/settings/get"
        assert json.loads(request.content) == {"keys": ["min_search_interval"]}
        return httpx.Response(200, json={"min_search_interval": 45})

    client = _client(handler)
    await client.authorize()
    settings = await client.get_settings(["min_search_interval"])
    await client.aclose()

    assert settings == [45]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([45], "invalid settings response"),
        ({}, "invalid settings response"),
        (
            {"min_search_interval": 45, "unexpected": 1},
            "invalid settings response",
        ),
        ({"min_search_interval": None}, "invalid settings value"),
    ],
)
async def test_get_settings_rejects_noncanonical_responses(
    payload: object,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sessions/authorize":
            return httpx.Response(200, json=_auth())
        return httpx.Response(200, json=payload)

    client = _client(handler)
    await client.authorize()
    with pytest.raises(AirDcppResponseError, match=message):
        await client.get_settings(["min_search_interval"])
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, {"message": "bad local-password"}, AirDcppAuthenticationError),
        (
            403,
            {"error": {"message": "The permission queue_view is required"}},
            AirDcppPermissionError,
        ),
        (404, {"message": "missing private entity"}, AirDcppEntityNotFoundError),
        (409, {"message": "conflict private entity"}, AirDcppConflictError),
        (422, {"message": "invalid private entity"}, AirDcppConflictError),
        (429, {"message": "slow down private entity"}, AirDcppRateLimitError),
        (500, {"message": "server private entity"}, AirDcppUnavailableError),
    ],
)
async def test_http_errors_are_typed_and_do_not_leak_raw_values(
    status: int,
    body: dict[str, object],
    error_type: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"Retry-After": "12"})

    client = _client(handler)
    with pytest.raises(error_type) as raised:
        await client.authorize()
    await client.aclose()

    rendered = f"{raised.value!s} {raised.value!r}"
    assert "local-password" not in rendered
    assert "private entity" not in rendered
    if isinstance(raised.value, AirDcppPermissionError):
        assert raised.value.missing_permission == "queue_view"
    if isinstance(raised.value, AirDcppRateLimitError):
        assert raised.value.retry_after_seconds == 12


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b""),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"session_id": 123}),
        httpx.Response(302, headers={"Location": "http://other.test/api/v1/"}),
    ],
)
async def test_invalid_responses_fail_closed_without_body_leak(
    response: httpx.Response,
) -> None:
    client = _client(lambda _request: response)
    with pytest.raises(AirDcppResponseError) as raised:
        await client.authorize()
    await client.aclose()

    assert "not-json" not in str(raised.value)
    assert "other.test" not in str(raised.value)


async def test_incompatible_response_reports_only_safe_contract_location() -> None:
    payload = _auth()
    payload["user"] = {
        "username": "pullbox",
        "permissions": ["search", {"private_value": "must-not-leak"}],
    }
    client = _client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(AirDcppResponseError) as raised:
        await client.authorize()
    await client.aclose()

    message = str(raised.value)
    assert message == (
        "AirDC++ returned an incompatible AirDcppAuthenticationInfo response "
        "(user.permissions.1: string_type)"
    )
    assert "private_value" not in message
    assert "must-not-leak" not in message
    assert "server-bearer-token" not in message


async def test_oversized_response_fails_before_json_parsing() -> None:
    client = _client(
        lambda _request: httpx.Response(200, content=b"x" * 65),
        max_response_bytes=64,
    )

    with pytest.raises(AirDcppResponseError, match="response exceeded"):
        await client.authorize()
    await client.aclose()


async def test_transport_timeout_is_normalized_without_endpoint_or_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    client = _client(handler)
    with pytest.raises(AirDcppUnavailableError) as raised:
        await client.authorize()
    await client.aclose()

    rendered = str(raised.value)
    assert "private timeout detail" not in rendered
    assert "local-password" not in rendered
    assert "airdcpp.test" not in rendered


def test_client_uses_explicit_timeouts_and_bounded_pool() -> None:
    client = _client(lambda _request: httpx.Response(204))

    assert client.timeout.connect == 15
    assert client.timeout.read == 15
    assert client.max_connections == 4
    assert client.max_keepalive_connections == 2


async def test_search_instance_lifecycle_uses_bounded_exact_rest_paths() -> None:
    calls: list[tuple[str, str, object | None]] = []
    grouped_result = {
        "id": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "name": "Example Comic 001.cbz",
        "relevance": 1.0,
        "hits": 1,
        "users": {"count": 1},
        "type": {"id": "file"},
        "path": "/private/peer/path",
        "tth": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "time": 0,
        "slots": {"free": 1, "total": 1, "str": "1/1"},
        "connection": 1,
        "size": 100,
    }
    instance = {
        "id": 44,
        "expires_in": 60_000,
        "current_search_id": 0,
        "owner": "session:123:pullbox",
        "queue_time": 0,
        "queued_count": 0,
        "result_count": 1,
        "searches_sent_ago": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/v1/sessions/authorize":
            return httpx.Response(200, json=_auth())
        if request.url.path == "/api/v1/search" and request.method == "POST":
            return httpx.Response(200, json=instance)
        if request.url.path == "/api/v1/search/44" and request.method == "GET":
            return httpx.Response(200, json=instance)
        if request.url.path == "/api/v1/search/44/results/0/100":
            return httpx.Response(200, json=[grouped_result])
        if request.url.path == "/api/v1/search/44" and request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    client = _client(handler)
    await client.authorize()
    created = await client.create_search_instance(expiration_minutes=5, owner_suffix="pullbox")
    loaded = await client.get_search_instance(created.id)
    results = await client.get_search_results(created.id, start=0, count=100)
    await client.delete_search_instance(created.id)
    await client.aclose()

    assert loaded.result_count == 1
    assert results[0].tth == "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"
    assert calls[1] == (
        "POST",
        "/api/v1/search",
        {"expiration": 5, "owner_suffix": "pullbox"},
    )


@pytest.mark.parametrize(
    ("start", "count"),
    [(-1, 10), (0, 0), (0, 101)],
)
async def test_search_result_page_rejects_unbounded_ranges(start: int, count: int) -> None:
    client = _client(lambda _request: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="search result page"):
        await client.get_search_results(1, start=start, count=count)
    await client.aclose()
