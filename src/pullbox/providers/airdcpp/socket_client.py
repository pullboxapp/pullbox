"""Bounded asynchronous AirDC++ WebSocket client."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from pullbox.core.url_validation import normalize_peer_base_url
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
    from pydantic import SecretStr

_PERMISSION_PATTERN = re.compile(r"permission\s+([a-z_]+)\s+is\s+required", re.IGNORECASE)
_KNOWN_PERMISSIONS = frozenset(
    {
        "admin",
        "download",
        "hubs_edit",
        "hubs_send",
        "hubs_view",
        "queue_edit",
        "queue_view",
        "search",
        "settings_edit",
        "settings_view",
        "transfers",
    }
)
_LISTENER_PATH = re.compile(
    r"^/(?P<section>[a-z_]+)(?:/(?P<entity_id>[1-9][0-9]*))?/listeners/"
    r"(?P<event>[a-z_]+)$"
)


class AirDcppSocket(Protocol):
    """Transport subset used by the protocol client and deterministic fakes."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


AirDcppSocketFactory = Callable[[str], Awaitable[AirDcppSocket]]
AirDcppEventHandler = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Subscription:
    path: str
    event: str
    entity_id: int | None
    handler: AirDcppEventHandler


@dataclass(frozen=True, slots=True)
class _Event:
    name: str
    entity_id: int | None
    data: Any


class AirDcppSocketClient:
    """Own one authenticated AirDC++ WebSocket connection.

    Callback futures, event buffering, and subscriptions are all explicitly
    bounded. Authentication tokens are used only in the association frame and
    are never retained after ``connect`` returns.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        callback_limit: int = 256,
        event_queue_limit: int = 1000,
        socket_factory: AirDcppSocketFactory | None = None,
    ) -> None:
        if callback_limit < 1 or event_queue_limit < 1:
            raise ValueError("AirDC++ socket bounds must be positive")
        normalized = normalize_peer_base_url(base_url, reject_query_or_fragment=True)
        parsed = urlsplit(normalized)
        socket_scheme = "wss" if parsed.scheme == "https" else "ws"
        self._uri = urlunsplit((socket_scheme, parsed.netloc, "/api/v1/", "", ""))
        self._timeout_seconds = timeout_seconds
        self._callback_limit = callback_limit
        self._event_queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=event_queue_limit)
        self._socket_factory = socket_factory or _open_socket

        self._socket: AirDcppSocket | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._disconnect_signal: asyncio.Future[Exception | None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        self._active_subscriptions: set[str] = set()
        self._next_callback_id = 1
        self._associated = False
        self._closing = False
        self._malformed_frame_count = 0
        self._dropped_event_count = 0

    @property
    def pending_callback_count(self) -> int:
        return len(self._pending)

    @property
    def desired_subscription_count(self) -> int:
        return len(self._subscriptions)

    @property
    def active_subscription_count(self) -> int:
        return len(self._active_subscriptions)

    @property
    def malformed_frame_count(self) -> int:
        return self._malformed_frame_count

    @property
    def dropped_event_count(self) -> int:
        return self._dropped_event_count

    @property
    def event_queue_size(self) -> int:
        return self._event_queue.qsize()

    @property
    def connected(self) -> bool:
        return self._associated and self._socket is not None

    @property
    def background_task_count(self) -> int:
        return sum(
            task is not None and not task.done()
            for task in (self._receiver_task, self._dispatcher_task)
        )

    async def connect(self, auth_token: SecretStr) -> None:
        """Open, associate, and replay all desired subscriptions."""
        if self._closing:
            raise AirDcppUnavailableError
        if self.connected:
            return

        self._active_subscriptions.clear()
        loop = asyncio.get_running_loop()
        self._disconnect_signal = loop.create_future()
        try:
            self._socket = await self._socket_factory(self._uri)
        except (AirDcppResponseError, AirDcppUnavailableError):
            raise
        except Exception as exc:
            raise AirDcppUnavailableError from exc

        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_events(), name="airdcpp-event-dispatch"
            )
        self._receiver_task = asyncio.create_task(
            self._receive_messages(), name="airdcpp-socket-receive"
        )

        try:
            await self.request(
                "POST",
                "/sessions/socket",
                {"auth_token": auth_token.get_secret_value()},
            )
            self._associated = True
            for subscription in tuple(self._subscriptions.values()):
                await self._activate_subscription(subscription)
        except BaseException:
            await self._disconnect(AirDcppUnavailableError())
            raise

    async def request(
        self,
        method: str,
        path: str,
        data: object | None = None,
    ) -> object:
        """Send one socket request and route its out-of-order callback."""
        socket = self._socket
        if socket is None:
            raise AirDcppUnavailableError
        if len(self._pending) >= self._callback_limit:
            raise AirDcppResponseError("AirDC++ socket callback capacity was reached")
        if not path.startswith("/") or method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ValueError("Invalid AirDC++ socket request")

        callback_id = self._next_callback_id
        self._next_callback_id += 1
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[callback_id] = future
        frame: dict[str, object] = {
            "method": method,
            "path": path,
            "callback_id": callback_id,
        }
        if data is not None:
            frame["data"] = data

        try:
            await socket.send(json.dumps(frame, separators=(",", ":")))
        except Exception as exc:
            self._pending.pop(callback_id, None)
            future.cancel()
            await self._disconnect(AirDcppUnavailableError())
            raise AirDcppUnavailableError from exc

        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await asyncio.shield(future)
        except TimeoutError as exc:
            self._pending.pop(callback_id, None)
            future.cancel()
            raise AirDcppUnavailableError from exc
        finally:
            self._pending.pop(callback_id, None)

    async def subscribe(self, path: str, handler: AirDcppEventHandler) -> None:
        """Retain and, when connected, activate one event subscription."""
        match = _LISTENER_PATH.fullmatch(path)
        if match is None:
            raise ValueError("Invalid AirDC++ listener path")
        entity = match.group("entity_id")
        subscription = _Subscription(
            path=path,
            event=match.group("event"),
            entity_id=int(entity) if entity is not None else None,
            handler=handler,
        )
        self._subscriptions[path] = subscription
        if self.connected:
            await self._activate_subscription(subscription)

    async def unsubscribe(self, path: str) -> None:
        """Remove one desired listener and deactivate it when connected."""
        self._subscriptions.pop(path, None)
        if path in self._active_subscriptions and self.connected:
            await self.request("DELETE", path)
        self._active_subscriptions.discard(path)

    async def wait_disconnected(self) -> None:
        """Wait until the active transport disconnects."""
        signal = self._disconnect_signal
        if signal is None:
            raise AirDcppUnavailableError
        error = await asyncio.shield(signal)
        if error is not None:
            raise error

    async def close(self) -> None:
        """Close the socket and cancel all owned work without leaking tasks."""
        if self._closing:
            return
        self._closing = True
        socket = self._socket
        self._socket = None
        self._associated = False
        self._active_subscriptions.clear()
        if socket is not None:
            with suppress(Exception):
                await socket.close()

        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._receiver_task, self._dispatcher_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._receiver_task = None
        self._dispatcher_task = None
        self._reject_pending(AirDcppUnavailableError())
        while not self._event_queue.empty():
            with suppress(asyncio.QueueEmpty):
                self._event_queue.get_nowait()
        if self._disconnect_signal is not None and not self._disconnect_signal.done():
            self._disconnect_signal.set_result(None)

    async def _activate_subscription(self, subscription: _Subscription) -> None:
        await self.request("POST", subscription.path)
        self._active_subscriptions.add(subscription.path)

    async def _receive_messages(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            while True:
                raw = await socket.recv()
                self._receive_frame(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._disconnect(AirDcppUnavailableError())

    def _receive_frame(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._malformed_frame_count += 1
            return
        if not isinstance(payload, dict):
            self._malformed_frame_count += 1
            return
        if "callback_id" in payload:
            self._receive_callback(payload)
            return
        if "event" in payload:
            self._receive_event(payload)
            return
        self._malformed_frame_count += 1

    def _receive_callback(self, payload: dict[str, Any]) -> None:
        callback_id = payload.get("callback_id")
        if type(callback_id) is not int or callback_id <= 0:
            self._malformed_frame_count += 1
            return
        future = self._pending.get(callback_id)
        if future is None or future.done():
            return
        code = payload.get("code")
        if type(code) is not int or not 100 <= code <= 599:
            future.set_exception(AirDcppResponseError("AirDC++ returned an invalid callback"))
            return
        if 200 <= code < 300:
            future.set_result(payload.get("data"))
            return
        future.set_exception(_response_error(code, payload.get("error")))

    def _receive_event(self, payload: dict[str, Any]) -> None:
        name = payload.get("event")
        entity_id = payload.get("id")
        if (
            not isinstance(name, str)
            or not name
            or (entity_id is not None and (type(entity_id) is not int or entity_id <= 0))
        ):
            self._malformed_frame_count += 1
            return
        event = _Event(name=name, entity_id=entity_id, data=payload.get("data"))
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_event_count += 1

    async def _dispatch_events(self) -> None:
        while True:
            event = await self._event_queue.get()
            subscriptions = tuple(self._subscriptions.values())
            for subscription in subscriptions:
                if subscription.event != event.name:
                    continue
                if subscription.entity_id is not None and subscription.entity_id != event.entity_id:
                    continue
                try:
                    await subscription.handler(event.data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Consumer failures must not terminate dispatch or expose
                    # untrusted event payloads in normal logs.
                    continue

    async def _disconnect(self, error: Exception) -> None:
        self._associated = False
        self._active_subscriptions.clear()
        socket = self._socket
        self._socket = None
        if socket is not None:
            with suppress(Exception):
                await socket.close()
        self._reject_pending(error)
        if self._disconnect_signal is not None and not self._disconnect_signal.done():
            self._disconnect_signal.set_result(error)

    def _reject_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


async def _open_socket(uri: str) -> AirDcppSocket:
    """Open a production socket with bounded protocol buffers."""
    from websockets.asyncio.client import connect

    try:
        return await connect(
            uri,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_048_576,
            max_queue=16,
            proxy=None,
        )
    except Exception as exc:
        raise AirDcppUnavailableError from exc


def _response_error(code: int, payload: object) -> Exception:
    if code == 401:
        return AirDcppAuthenticationError()
    if code == 403:
        permission: str | None = None
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                match = _PERMISSION_PATTERN.search(message)
                if match and match.group(1).lower() in _KNOWN_PERMISSIONS:
                    permission = match.group(1).lower()
        return AirDcppPermissionError(permission)
    if code == 404:
        return AirDcppEntityNotFoundError()
    if code in {409, 422}:
        return AirDcppConflictError()
    if code == 429:
        return AirDcppRateLimitError()
    if code >= 500:
        return AirDcppUnavailableError()
    return AirDcppResponseError("AirDC++ rejected the socket request")
