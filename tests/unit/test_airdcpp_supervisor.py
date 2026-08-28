"""AirDC++ supervisor state, reconnect, and registry contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from pullbox.providers.airdcpp.errors import (
    AirDcppAuthenticationError,
    AirDcppUnavailableError,
)
from pullbox.providers.airdcpp.supervisor import (
    AirDcppSupervisor,
    AirDcppSupervisorConfig,
    AirDcppSupervisorRegistry,
    AirDcppSupervisorState,
)


def _auth(
    *,
    permissions: tuple[str, ...] = (
        "search",
        "download",
        "queue_view",
        "queue_edit",
        "hubs_view",
        "settings_view",
    ),
):
    return SimpleNamespace(
        auth_token=SecretStr("ephemeral-token"),
        system_info=SimpleNamespace(api_version=1, api_feature_level=10),
        user=SimpleNamespace(permissions=list(permissions)),
    )


class _FakeApi:
    instances: ClassVar[list[_FakeApi]] = []

    def __init__(
        self,
        *,
        authorize_error: Exception | None = None,
        auth: Any = None,
        minimum_search_interval: int = 45,
    ) -> None:
        self.authorize_error = authorize_error
        self.auth = auth or _auth()
        self.authorize_calls = 0
        self.minimum_search_interval = minimum_search_interval
        self.deleted = 0
        self.closed = False
        self.__class__.instances.append(self)

    async def authorize(self):
        self.authorize_calls += 1
        if self.authorize_error is not None:
            raise self.authorize_error
        return self.auth

    async def get_current_session(self):
        return SimpleNamespace(user=self.auth.user)

    async def get_system_info(self):
        return self.auth.system_info

    async def get_settings(self, _keys: list[str]):
        return [self.minimum_search_interval]

    async def delete_current_session(self) -> None:
        self.deleted += 1

    async def aclose(self) -> None:
        self.closed = True


class _FakeSocket:
    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.connect_calls = 0
        self.close_calls = 0
        self.disconnected = asyncio.Event()
        self.subscriptions: list[tuple[str, object]] = []

    async def connect(self, _token: SecretStr) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def subscribe(self, path: str, handler: object) -> None:
        self.subscriptions.append((path, handler))

    async def unsubscribe(self, path: str) -> None:
        self.subscriptions = [item for item in self.subscriptions if item[0] != path]

    async def wait_disconnected(self) -> None:
        await self.disconnected.wait()
        raise AirDcppUnavailableError

    async def close(self) -> None:
        self.close_calls += 1
        self.disconnected.set()


def _config(config_id: int = 7, *, enabled: bool = True) -> AirDcppSupervisorConfig:
    return AirDcppSupervisorConfig(
        config_id=config_id,
        client_identity=f"airdcpp:{config_id}",
        name=f"Air {config_id}",
        base_url="http://air.example.test:5600",
        username="pullbox",
        password=SecretStr("password"),
        request_timeout_seconds=15,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_supervisor_start_is_nonblocking_and_projects_ready_health() -> None:
    api = _FakeApi()
    socket = _FakeSocket()
    reconciliations: list[int] = []

    async def reconcile(config_id: int) -> None:
        reconciliations.append(config_id)

    supervisor = AirDcppSupervisor(
        config=_config(),
        api_client=api,
        socket_client=socket,
        reconcile=reconcile,
    )

    await supervisor.subscribe("/queue/listeners/bundle_updated", lambda _data: asyncio.sleep(0))
    supervisor.start()
    await supervisor.wait_for_state(AirDcppSupervisorState.READY, timeout_seconds=1)

    health = supervisor.health
    assert health.state is AirDcppSupervisorState.READY
    assert health.compatible is True
    assert health.missing_permissions == ()
    assert health.reconnect_attempts == 0
    assert health.last_ready_at is not None
    assert socket.subscriptions[0][0] == "/queue/listeners/bundle_updated"
    queue_paths = {path for path, _handler in socket.subscriptions}
    assert {
        "/queue/listeners/queue_bundle_added",
        "/queue/listeners/queue_bundle_status",
        "/queue/listeners/queue_bundle_priority",
        "/queue/listeners/queue_bundle_tick",
        "/queue/listeners/queue_bundle_sources",
        "/queue/listeners/queue_bundle_removed",
    }.issubset(queue_paths)
    assert reconciliations == [7]
    assert supervisor.background_task_count == 1

    await supervisor.stop()
    assert supervisor.state is AirDcppSupervisorState.DISABLED
    assert supervisor.background_task_count == 0
    assert socket.close_calls == 1
    assert api.deleted == 1
    assert api.closed is True


@pytest.mark.asyncio
async def test_queue_event_storm_coalesces_to_one_owned_reconciliation() -> None:
    api = _FakeApi()
    socket = _FakeSocket()
    reconciliations: list[int] = []

    async def reconcile(config_id: int) -> None:
        reconciliations.append(config_id)

    supervisor = AirDcppSupervisor(
        config=_config(),
        api_client=api,
        socket_client=socket,
        reconcile=reconcile,
    )
    supervisor.start()
    await supervisor.wait_for_state(AirDcppSupervisorState.READY, timeout_seconds=1)
    handler = next(
        handler
        for path, handler in socket.subscriptions
        if path == "/queue/listeners/queue_bundle_tick"
    )

    await asyncio.gather(*(handler({"id": 91}) for _ in range(10_000)))
    await asyncio.sleep(0)

    assert reconciliations == [7, 7]
    await supervisor.stop()
    assert supervisor.background_task_count == 0


@pytest.mark.asyncio
async def test_supervisor_retries_unavailable_socket_with_one_owned_runner() -> None:
    api = _FakeApi()
    socket = _FakeSocket(connect_error=AirDcppUnavailableError())
    delays: list[float] = []
    continue_retry = asyncio.Event()

    async def delay(seconds: float) -> None:
        delays.append(seconds)
        await continue_retry.wait()

    supervisor = AirDcppSupervisor(
        config=_config(),
        api_client=api,
        socket_client=socket,
        sleep=delay,
        jitter=lambda seconds: seconds,
    )
    supervisor.start()
    supervisor.start()
    await supervisor.wait_for_state(AirDcppSupervisorState.DEGRADED_SOCKET, timeout_seconds=1)

    assert delays == [1.0]
    assert supervisor.health.reconnect_attempts == 1
    assert supervisor.background_task_count == 1
    await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_fails_closed_when_airdcpp_hub_interval_is_below_45() -> None:
    api = _FakeApi(minimum_search_interval=31)
    socket = _FakeSocket()
    supervisor = AirDcppSupervisor(
        config=_config(),
        api_client=api,
        socket_client=socket,
    )

    supervisor.start()
    await supervisor.wait_for_state(
        AirDcppSupervisorState.UNSAFE_SEARCH_INTERVAL,
        timeout_seconds=1,
    )

    assert supervisor.health.remote_min_search_interval_seconds == 31
    assert supervisor.health.last_error_code == "unsafe_search_interval"
    assert socket.connect_calls == 0
    await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_classifies_authentication_and_incompatibility_without_spinning() -> None:
    auth_api = _FakeApi(authorize_error=AirDcppAuthenticationError())
    auth_supervisor = AirDcppSupervisor(
        config=_config(),
        api_client=auth_api,
        socket_client=_FakeSocket(),
    )
    auth_supervisor.start()
    await auth_supervisor.wait_for_state(
        AirDcppSupervisorState.AUTHENTICATION_FAILED,
        timeout_seconds=1,
    )
    assert auth_api.authorize_calls == 1
    await auth_supervisor.stop()

    incompatible_api = _FakeApi(auth=_auth())
    incompatible_api.auth.system_info.api_feature_level = 9
    incompatible_supervisor = AirDcppSupervisor(
        config=_config(8),
        api_client=incompatible_api,
        socket_client=_FakeSocket(),
    )
    incompatible_supervisor.start()
    await incompatible_supervisor.wait_for_state(
        AirDcppSupervisorState.INCOMPATIBLE,
        timeout_seconds=1,
    )
    assert incompatible_api.authorize_calls == 1
    await incompatible_supervisor.stop()


@pytest.mark.asyncio
async def test_disabled_supervisor_starts_no_background_or_network_work() -> None:
    api = _FakeApi()
    socket = _FakeSocket()
    supervisor = AirDcppSupervisor(
        config=_config(enabled=False),
        api_client=api,
        socket_client=socket,
    )

    supervisor.start()
    await asyncio.sleep(0)

    assert supervisor.state is AirDcppSupervisorState.DISABLED
    assert supervisor.background_task_count == 0
    assert api.authorize_calls == 0
    assert socket.connect_calls == 0
    await supervisor.stop()


class _RegistrySupervisor:
    def __init__(self, config: AirDcppSupervisorConfig) -> None:
        self.config = config
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.mark.asyncio
async def test_registry_replaces_only_changed_exact_client_and_stops_all() -> None:
    created: list[_RegistrySupervisor] = []

    def factory(config: AirDcppSupervisorConfig) -> _RegistrySupervisor:
        supervisor = _RegistrySupervisor(config)
        created.append(supervisor)
        return supervisor

    registry = AirDcppSupervisorRegistry(supervisor_factory=factory)
    await registry.apply((_config(1), _config(2)))
    first, second = created

    await registry.apply((_config(1), _config(2)))
    assert len(created) == 2

    changed = replace(_config(2), request_timeout_seconds=30)
    await registry.apply((_config(1), changed))

    assert first.stopped == 0
    assert second.stopped == 1
    assert len(created) == 3
    assert registry.config_ids == (1, 2)

    await registry.stop()
    assert first.stopped == 1
    assert created[-1].stopped == 1
    assert registry.config_ids == ()
