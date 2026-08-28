"""AirDC++ WebSocket protocol and lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import SecretStr

from pullbox.providers.airdcpp.errors import (
    AirDcppPermissionError,
    AirDcppResponseError,
    AirDcppUnavailableError,
)
from pullbox.providers.airdcpp.socket_client import AirDcppSocketClient


class _ClosedError(Exception):
    pass


class _FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def recv(self) -> str:
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(_ClosedError())

    async def reply(self, callback_id: int, *, code: int = 200, data: object = None) -> None:
        payload: dict[str, object] = {"code": code, "callback_id": callback_id}
        if data is not None:
            payload["data"] = data
        await self.incoming.put(json.dumps(payload))

    async def event(self, event: str, data: object, *, entity_id: int | None = None) -> None:
        payload: dict[str, object] = {"event": event, "data": data}
        if entity_id is not None:
            payload["id"] = entity_id
        await self.incoming.put(json.dumps(payload))


async def _wait_for_sent(socket: _FakeSocket, count: int) -> None:
    async with asyncio.timeout(1):
        while len(socket.sent) < count:
            await asyncio.sleep(0)


async def _connect(client: AirDcppSocketClient, socket: _FakeSocket) -> None:
    task = asyncio.create_task(client.connect(SecretStr("do-not-log-token")))
    await _wait_for_sent(socket, 1)
    association = socket.sent[0]
    assert association == {
        "method": "POST",
        "path": "/sessions/socket",
        "callback_id": 1,
        "data": {"auth_token": "do-not-log-token"},
    }
    await socket.reply(1, code=204)
    await task


@pytest.mark.asyncio
async def test_socket_associates_and_routes_out_of_order_callbacks() -> None:
    socket = _FakeSocket()
    requested_uris: list[str] = []

    async def connect(uri: str) -> _FakeSocket:
        requested_uris.append(uri)
        return socket

    client = AirDcppSocketClient(
        base_url="https://air.example.test:5601",
        timeout_seconds=1,
        socket_factory=connect,
    )
    await _connect(client, socket)

    first = asyncio.create_task(client.request("GET", "/system/system_info"))
    second = asyncio.create_task(client.request("GET", "/hubs"))
    await _wait_for_sent(socket, 3)

    await socket.reply(3, data={"kind": "second"})
    await socket.reply(2, data={"kind": "first"})

    assert requested_uris == ["wss://air.example.test:5601/api/v1/"]
    assert await first == {"kind": "first"}
    assert await second == {"kind": "second"}
    assert client.pending_callback_count == 0
    await client.close()


@pytest.mark.asyncio
async def test_socket_maps_safe_error_and_rejects_malformed_response() -> None:
    socket = _FakeSocket()

    async def connect(_uri: str) -> _FakeSocket:
        return socket

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        socket_factory=connect,
    )
    await _connect(client, socket)

    denied = asyncio.create_task(client.request("POST", "/search"))
    await _wait_for_sent(socket, 2)
    await socket.incoming.put(
        json.dumps(
            {
                "code": 403,
                "callback_id": 2,
                "error": {"message": "Permission search is required: secret-details"},
            }
        )
    )
    with pytest.raises(AirDcppPermissionError) as denied_error:
        await denied
    assert str(denied_error.value) == "AirDC++ requires permission: search"

    malformed = asyncio.create_task(client.request("GET", "/hubs"))
    await _wait_for_sent(socket, 3)
    await socket.incoming.put(json.dumps({"code": "200", "callback_id": 3}))
    with pytest.raises(AirDcppResponseError):
        await malformed

    await client.close()


@pytest.mark.asyncio
async def test_subscriptions_dispatch_bounded_events_and_replay_after_reconnect() -> None:
    first_socket = _FakeSocket()
    second_socket = _FakeSocket()
    sockets = iter((first_socket, second_socket))

    async def connect(_uri: str) -> _FakeSocket:
        return next(sockets)

    received: list[int] = []

    async def on_result(data: dict[str, Any]) -> None:
        received.append(data["sequence"])

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        event_queue_limit=2,
        socket_factory=connect,
    )
    await _connect(client, first_socket)

    subscribe = asyncio.create_task(
        client.subscribe("/search/7/listeners/search_result_added", on_result)
    )
    await _wait_for_sent(first_socket, 2)
    await first_socket.reply(2, code=204)
    await subscribe

    await first_socket.event("search_result_added", {"sequence": 1}, entity_id=8)
    await first_socket.event("search_result_added", {"sequence": 2}, entity_id=7)
    async with asyncio.timeout(1):
        while received != [2]:
            await asyncio.sleep(0)

    await first_socket.incoming.put(_ClosedError())
    with pytest.raises(AirDcppUnavailableError):
        await client.wait_disconnected()

    reconnect = asyncio.create_task(client.connect(SecretStr("replacement-token")))
    await _wait_for_sent(second_socket, 1)
    await second_socket.reply(3, code=200)
    await _wait_for_sent(second_socket, 2)
    assert second_socket.sent[1]["path"] == "/search/7/listeners/search_result_added"
    await second_socket.reply(4, code=204)
    await reconnect

    assert client.desired_subscription_count == 1
    assert client.active_subscription_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_disconnect_rejects_pending_callbacks_and_close_leaks_no_tasks() -> None:
    socket = _FakeSocket()

    async def connect(_uri: str) -> _FakeSocket:
        return socket

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        socket_factory=connect,
    )
    await _connect(client, socket)

    request = asyncio.create_task(client.request("GET", "/hubs"))
    await _wait_for_sent(socket, 2)
    await socket.incoming.put(_ClosedError())

    with pytest.raises(AirDcppUnavailableError):
        await request
    assert client.pending_callback_count == 0

    await client.close()
    assert socket.closed is True
    assert client.background_task_count == 0


@pytest.mark.asyncio
async def test_callback_map_is_hard_bounded() -> None:
    socket = _FakeSocket()

    async def connect(_uri: str) -> _FakeSocket:
        return socket

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        callback_limit=2,
        socket_factory=connect,
    )
    await _connect(client, socket)

    tasks = [asyncio.create_task(client.request("GET", f"/resource/{index}")) for index in range(2)]
    await _wait_for_sent(socket, 3)
    with pytest.raises(AirDcppResponseError, match="callback capacity"):
        await client.request("GET", "/one-too-many")

    await socket.reply(2, data={})
    await socket.reply(3, data={})
    await asyncio.gather(*tasks)
    await client.close()


@pytest.mark.asyncio
async def test_event_storm_and_malformed_or_unknown_frames_remain_bounded() -> None:
    socket = _FakeSocket()

    async def connect(_uri: str) -> _FakeSocket:
        return socket

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def slow_handler(_data: object) -> None:
        handler_started.set()
        await release_handler.wait()

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        event_queue_limit=2,
        socket_factory=connect,
    )
    await _connect(client, socket)
    subscribe = asyncio.create_task(
        client.subscribe("/queue/listeners/bundle_updated", slow_handler)
    )
    await _wait_for_sent(socket, 2)
    await socket.reply(2, code=204)
    await subscribe

    await socket.event("bundle_updated", {"sequence": 0})
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    for sequence in range(1, 10_001):
        await socket.event("bundle_updated", {"sequence": sequence})
    await socket.incoming.put("not-json")
    await socket.event("future_additive_event", {"ignored": True})
    async with asyncio.timeout(5):
        while client.dropped_event_count == 0 or client.malformed_frame_count == 0:
            await asyncio.sleep(0)

    assert client.event_queue_size <= 2
    assert client.dropped_event_count >= 9_998
    assert client.malformed_frame_count == 1

    release_handler.set()
    await client.close()


@pytest.mark.asyncio
async def test_reconnect_shutdown_stress_returns_to_owned_task_baseline() -> None:
    sockets = [_FakeSocket() for _ in range(25)]
    remaining = iter(sockets)

    async def connect(_uri: str) -> _FakeSocket:
        return next(remaining)

    client = AirDcppSocketClient(
        base_url="http://air.example.test",
        timeout_seconds=1,
        socket_factory=connect,
    )

    for index, socket in enumerate(sockets):
        task = asyncio.create_task(client.connect(SecretStr(f"token-{index}")))
        await _wait_for_sent(socket, 1)
        callback_id = socket.sent[0]["callback_id"]
        assert isinstance(callback_id, int)
        await socket.reply(callback_id, code=204)
        await task
        await socket.incoming.put(_ClosedError())
        with pytest.raises(AirDcppUnavailableError):
            await client.wait_disconnected()

    await client.close()
    assert client.background_task_count == 0
    assert client.pending_callback_count == 0
    assert all(socket.closed for socket in sockets)
