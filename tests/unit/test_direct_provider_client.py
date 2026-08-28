from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence  # noqa: TC003 - used by async fixture annotations
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

import pullbox.providers.direct.client as client_module
from pullbox.providers.direct.client import (
    DirectProviderClient,
    DirectProviderClientError,
)
from pullbox.providers.direct.contract import (
    DIRECT_PROVIDER_PROTOCOL_V1,
    DirectResolveRequest,
    DirectSearchRequest,
)

TOKEN = "provider-token-that-must-stay-redacted"


async def _resolve_public(_host: str, _port: int) -> Sequence[str]:
    return ["8.8.8.8"]


def _manifest() -> dict[str, object]:
    return {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "provider_id": "pullbox.synthetic",
        "display_name": "Synthetic Provider",
        "description": "Deterministic test provider.",
        "provider_version": "1.0.0",
        "supported_protocol_versions": [DIRECT_PROVIDER_PROTOCOL_V1],
        "publisher": "Pullbox",
        "license": "GPL-3.0-or-later",
        "source_domains": ["provider.test"],
        "capabilities": {
            "search": True,
            "resolve": True,
            "browser_challenge": False,
            "health": True,
            "quota": False,
            "configuration_schema": False,
        },
        "configuration_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _health() -> dict[str, object]:
    return {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "process_status": "healthy",
        "source_status": "healthy",
        "message": "Ready.",
        "retry_after_seconds": None,
        "diagnostics": {},
    }


def _search_response(request_id: str) -> dict[str, object]:
    return {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "request_id": request_id,
        "candidates": [
            {
                "provider_candidate_id": "candidate-1",
                "source_reference": "synthetic://candidate/1",
                "display_title": "Synthetic Adventures #1",
                "raw_title": "Synthetic Adventures 1 (2026)",
                "parsed": {"series_title": "Synthetic Adventures", "issue_numbers": ["1"]},
                "provider_confidence": 1.0,
                "provenance": {},
                "can_resolve": True,
            }
        ],
        "truncated": False,
    }


def _resolve_response(request_id: str) -> dict[str, object]:
    return {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "request_id": request_id,
        "artifacts": [
            {
                "artifact_id": "artifact-1",
                "coverage": {"issue_numbers": ["1"]},
                "route": "direct_artifact",
                "format": "cbz",
                "mirrors": [
                    {
                        "mirror_id": "mirror-1",
                        "host_kind": "generic_https",
                        "final_url": "https://files.example/1.cbz",
                    }
                ],
            }
        ],
    }


async def test_client_authenticates_and_validates_all_four_operations() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/manifest":
            return httpx.Response(200, json=_manifest())
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=_health())
        payload = json.loads(request.content)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json=_search_response(payload["request_id"]))
        return httpx.Response(200, json=_resolve_response(payload["request_id"]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        manifest = await client.manifest()
        health = await client.health()
        search_request = DirectSearchRequest(
            protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
            request_id=UUID("11111111-1111-4111-8111-111111111111"),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            intent={
                "series_title": "Synthetic Adventures",
                "normalized_title": "synthetic adventures",
                "issue_number": "1",
            },
        )
        search = await client.search(search_request)
        resolve_request = DirectResolveRequest(
            protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
            request_id=UUID("22222222-2222-4222-8222-222222222222"),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            provider_candidate_id="candidate-1",
        )
        resolve = await client.resolve(resolve_request)

    assert manifest.provider_id == "pullbox.synthetic"
    assert health.process_status == "healthy"
    assert search.request_id == search_request.request_id
    assert resolve.request_id == resolve_request.request_id
    assert seen_paths == ["/v1/manifest", "/v1/health", "/v1/search", "/v1/resolve"]


async def test_owned_http_client_uses_the_configured_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeout: httpx.Timeout | None = None
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        nonlocal captured_timeout
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, httpx.Timeout)
        captured_timeout = timeout
        return real_async_client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_manifest())),
            **kwargs,
        )

    monkeypatch.setattr(client_module.httpx, "AsyncClient", client_factory)
    client = DirectProviderClient(
        endpoint="https://provider.example",
        bearer_token=TOKEN,
        resolver=_resolve_public,
        request_timeout_seconds=45.0,
    )
    try:
        assert captured_timeout is not None
        assert captured_timeout.read == 45.0
    finally:
        await client.aclose()


async def test_client_revalidates_dns_before_every_operation() -> None:
    resolutions = 0

    async def resolver(_host: str, _port: int) -> Sequence[str]:
        nonlocal resolutions
        resolutions += 1
        return ["8.8.8.8"]

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=_manifest()))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=resolver,
            http_client=http_client,
        )
        await client.manifest()
        await client.manifest()

    assert resolutions == 2


async def test_client_connects_to_the_validated_address_with_original_host_and_tls_sni() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://8.8.8.8/v1/manifest"
        assert request.headers["Host"] == "provider.example"
        assert request.extensions["sni_hostname"] == "provider.example"
        return httpx.Response(200, json=_manifest())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        await client.manifest()


async def test_client_preserves_private_provider_port_when_pinning_the_address() -> None:
    async def resolver(_host: str, _port: int) -> Sequence[str]:
        return ["172.20.0.8"]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://172.20.0.8:8780/v1/manifest"
        assert request.headers["Host"] == "provider:8780"
        assert "sni_hostname" not in request.extensions
        return httpx.Response(200, json=_manifest())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="http://provider:8780",
            bearer_token=TOKEN,
            allow_private_http=True,
            resolver=resolver,
            http_client=http_client,
        )
        await client.manifest()


async def test_client_rejects_redirects_and_oversized_responses() -> None:
    redirect = httpx.MockTransport(
        lambda _request: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
    )
    async with httpx.AsyncClient(transport=redirect) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        with pytest.raises(DirectProviderClientError) as exc_info:
            await client.manifest()
        assert exc_info.value.code == "provider_redirect_rejected"

    oversized = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"{" + (b" " * (2 * 1024 * 1024)) + b"}",
        )
    )
    async with httpx.AsyncClient(transport=oversized) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        with pytest.raises(DirectProviderClientError) as exc_info:
            await client.manifest()
        assert exc_info.value.code == "provider_response_too_large"


async def test_client_classifies_authentication_and_malformed_responses_without_secrets() -> None:
    for response, expected_code in (
        (
            httpx.Response(401, json={"error": {"code": "bad", "message": TOKEN}}),
            "provider_authentication_failed",
        ),
        (httpx.Response(200, content=b"not-json"), "provider_malformed_response"),
    ):
        transport = httpx.MockTransport(lambda _request, value=response: value)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = DirectProviderClient(
                endpoint="https://provider.example",
                bearer_token=TOKEN,
                resolver=_resolve_public,
                http_client=http_client,
            )
            with pytest.raises(DirectProviderClientError) as exc_info:
                await client.manifest()
            assert exc_info.value.code == expected_code
            assert TOKEN not in str(exc_info.value)


async def test_client_preserves_bounded_browser_challenge_error_codes() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            503,
            json={
                "error": {
                    "code": "browser_challenge_required",
                    "message": TOKEN,
                }
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )

        with pytest.raises(DirectProviderClientError) as exc_info:
            await client.manifest()

    assert exc_info.value.code == "browser_challenge_required"
    assert exc_info.value.retryable is True
    assert TOKEN not in str(exc_info.value)


async def test_client_preserves_bounded_source_quota_retry_hint() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            429,
            json={
                "error": {
                    "code": "source_quota_limited",
                    "message": TOKEN,
                    "retry_after_seconds": 64_800,
                }
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )

        with pytest.raises(DirectProviderClientError) as exc_info:
            await client.manifest()

    assert exc_info.value.code == "source_quota_limited"
    assert exc_info.value.retry_after_seconds == 64_800
    assert TOKEN not in str(exc_info.value)


async def test_client_preserves_cooperative_cancellation() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200, json=_manifest())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        task = asyncio.create_task(client.manifest())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_client_logs_redacted_search_timing_and_result_count() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_search_response(json.loads(request.content)["request_id"]),
        )
    )
    structlog.reset_defaults()
    client_module.logger = structlog.get_logger(client_module.__name__)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
            provider_id="pullbox.synthetic",
        )
        request = DirectSearchRequest(
            protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
            request_id=UUID("11111111-1111-4111-8111-111111111111"),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            intent={
                "series_title": "Synthetic Adventures",
                "normalized_title": "synthetic adventures",
                "issue_number": "1",
            },
        )
        with capture_logs() as logs:
            await client.search(request)

    event = next(log for log in logs if log["event"] == "direct_provider_request_completed")
    assert event["operation"] == "search"
    assert event["provider_id"] == "pullbox.synthetic"
    assert event["request_id"] == str(request.request_id)
    assert event["protocol_version"] == DIRECT_PROVIDER_PROTOCOL_V1
    assert event["result_count"] == 1
    assert isinstance(event["duration_ms"], int | float)
    assert TOKEN not in str(event)


async def test_client_logs_only_classified_failure_details() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"error": {"message": TOKEN}},
        )
    )
    structlog.reset_defaults()
    client_module.logger = structlog.get_logger(client_module.__name__)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
            provider_id="pullbox.synthetic",
        )
        with capture_logs() as logs, pytest.raises(DirectProviderClientError):
            await client.manifest()

    event = next(log for log in logs if log["event"] == "direct_provider_request_failed")
    assert event["operation"] == "manifest"
    assert event["provider_id"] == "pullbox.synthetic"
    assert event["failure_code"] == "provider_authentication_failed"
    assert isinstance(event["duration_ms"], int | float)
    assert TOKEN not in str(event)


@pytest.mark.parametrize(
    ("status_code", "remote_code", "retryable"),
    [
        (429, "source_quota_limited", False),
        (401, "source_authentication_required", False),
        (503, "source_unavailable", True),
        (503, "source_malformed_response", False),
        (503, "source_contract_changed", False),
        (404, "candidate_not_found", False),
    ],
)
async def test_client_preserves_allowlisted_source_failure_codes(
    status_code: int,
    remote_code: str,
    retryable: bool,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status_code,
            json={"error": {"code": remote_code, "message": TOKEN}},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DirectProviderClient(
            endpoint="https://provider.example",
            bearer_token=TOKEN,
            resolver=_resolve_public,
            http_client=http_client,
        )
        request = DirectResolveRequest(
            protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
            request_id=UUID("22222222-2222-4222-8222-222222222222"),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            provider_candidate_id="candidate-1",
        )

        with pytest.raises(DirectProviderClientError) as exc_info:
            await client.resolve(request)

    assert exc_info.value.code == remote_code
    assert exc_info.value.retryable is retryable
    assert TOKEN not in str(exc_info.value)
