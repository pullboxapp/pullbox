"""Strict AirDC++ queue mutation and reconciliation wire contracts."""

from __future__ import annotations

import httpx
import pytest

from pullbox.providers.airdcpp.api_client import AirDcppApiClient
from pullbox.providers.airdcpp.contracts import (
    AirDcppQueueBundleAddInfo,
    AirDcppQueueFile,
    AirDcppSearchDownloadResponse,
)

_TTH = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"


def _queue_file_payload() -> dict[str, object]:
    return {
        "id": 51,
        "name": "Example Comic 001 (2026).cbz",
        "target": "/Downloads/Example Comic 001 (2026).cbz",
        "type": {"id": "file", "content_type": "file"},
        "bundle": 91,
        "size": 100_000_000.0,
        "downloaded_bytes": 25_000_000.0,
        "priority": {"id": 3, "str": "Normal", "auto": False},
        "time_added": 1.0,
        "time_finished": 0.0,
        "speed": 1_000_000.0,
        "seconds_left": 75.0,
        "sources": {"online": 1, "total": 2, "str": "1/2"},
        "status": {
            "id": "queued",
            "failed": False,
            "downloaded": False,
            "completed": False,
            "str": "Running",
        },
        "tth": _TTH,
    }


def test_queue_mutation_contracts_require_typed_bundle_and_file_identity() -> None:
    added = AirDcppQueueBundleAddInfo.model_validate({"id": 91, "merged": True})
    response = AirDcppSearchDownloadResponse.model_validate({"bundle_info": added.model_dump()})
    queue_file = AirDcppQueueFile.model_validate(_queue_file_payload())

    assert response.bundle_info == added
    assert response.directory_downloads is None
    assert queue_file.tth == _TTH
    assert queue_file.bundle_id == 91
    assert queue_file.size == 100_000_000
    assert queue_file.downloaded_bytes == 25_000_000
    assert queue_file.speed == 1_000_000
    assert queue_file.target.get_secret_value().startswith("/Downloads/")


@pytest.mark.asyncio
async def test_queue_client_uses_exact_bounded_endpoints_and_safe_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/sessions/authorize"):
            return httpx.Response(
                200,
                json={
                    "session_id": 1,
                    "auth_token": "secret-token",
                    "token_type": "Bearer",
                    "system_info": {
                        "api_version": 1,
                        "api_feature_level": 10,
                        "client_version": "2.14.0",
                        "platform": "linux",
                        "path_separator": "/",
                    },
                    "user": {"username": "pullbox", "permissions": ["download"]},
                    "wizard_pending": False,
                },
            )
        if path.endswith("/search/44/results/opaque/download"):
            return httpx.Response(200, json={"bundle_info": {"id": 91, "merged": False}})
        if path.endswith(f"/queue/files/{_TTH}"):
            return httpx.Response(200, json=[_queue_file_payload()])
        if path.endswith("/queue/bundles/91"):
            payload = _queue_file_payload()
            payload.pop("tth")
            payload.pop("bundle")
            payload["id"] = 91
            return httpx.Response(200, json=payload)
        if path.endswith("/queue/bundles/91/search"):
            return httpx.Response(204)
        if path.endswith("/queue/bundles/91/remove"):
            return httpx.Response(204)
        raise AssertionError(path)

    client = AirDcppApiClient(
        base_url="http://air.example.test:5600",
        username="pullbox",
        password="private-password",
        timeout_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    await client.authorize()

    added = await client.download_search_result(
        44,
        "opaque",
        target_name="Example Comic 001 (2026).cbz",
        priority=3,
    )
    files = await client.get_queue_files_by_tth(_TTH)
    bundle = await client.get_queue_bundle(91)
    await client.search_queue_bundle(91)
    await client.remove_queue_bundle(91)

    assert added == AirDcppQueueBundleAddInfo(id=91, merged=False)
    assert files[0].bundle_id == bundle.id == 91
    download_request = requests[1]
    assert download_request.method == "POST"
    assert download_request.url.path.endswith("/search/44/results/opaque/download")
    assert download_request.read() == (
        b'{"target_name":"Example Comic 001 (2026).cbz","priority":3}'
    )
    assert requests[-1].read() == b'{"remove_finished":false}'
    assert all(b"private-password" not in request.url.query for request in requests)
    await client.aclose()
