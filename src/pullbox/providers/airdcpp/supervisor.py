"""AirDC++ connection supervisor and exact-client registry."""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pullbox.providers.airdcpp.errors import (
    AirDcppAuthenticationError,
    AirDcppCompatibilityError,
    AirDcppPermissionError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from pydantic import SecretStr

    from pullbox.providers.airdcpp.contracts import (
        AirDcppAuthenticationInfo,
        AirDcppQueueBundleAddInfo,
        AirDcppQueueFile,
        AirDcppSession,
        AirDcppSystemInfo,
    )
    from pullbox.providers.airdcpp.socket_client import AirDcppEventHandler

_REQUIRED_PERMISSIONS = frozenset(
    {"search", "download", "queue_view", "queue_edit", "hubs_view", "settings_view"}
)
_API_VERSION = 1
_MINIMUM_FEATURE_LEVEL = 10
_QUEUE_LISTENER_PATHS = (
    "/queue/listeners/queue_bundle_added",
    "/queue/listeners/queue_bundle_status",
    "/queue/listeners/queue_bundle_priority",
    "/queue/listeners/queue_bundle_tick",
    "/queue/listeners/queue_bundle_sources",
    "/queue/listeners/queue_bundle_removed",
)


class AirDcppSupervisorState(StrEnum):
    """Safe externally visible lifecycle states for one exact client."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    COMPATIBLE_REST = "compatible_rest"
    SOCKET_CONNECTING = "socket_connecting"
    READY = "ready"
    DEGRADED_SOCKET = "degraded_socket"
    INCOMPATIBLE = "incompatible"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_FAILED = "permission_failed"
    UNSAFE_SEARCH_INTERVAL = "unsafe_search_interval"
    UNAVAILABLE = "unavailable"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class AirDcppSupervisorConfig:
    """Secret-safe immutable configuration used to detect exact changes."""

    config_id: int
    client_identity: str
    name: str
    base_url: str
    username: str
    password: SecretStr
    request_timeout_seconds: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.config_id <= 0:
            raise ValueError("AirDC++ config ID must be positive")
        if self.client_identity != f"airdcpp:{self.config_id}":
            raise ValueError("AirDC++ client identity must match the exact config")


@dataclass(frozen=True, slots=True)
class AirDcppSupervisorHealth:
    """Bounded state projection that contains no credentials or peer data."""

    state: AirDcppSupervisorState = AirDcppSupervisorState.DISABLED
    compatible: bool = False
    api_version: int | None = None
    api_feature_level: int | None = None
    missing_permissions: tuple[str, ...] = ()
    reconnect_attempts: int = 0
    last_ready_at: datetime | None = None
    last_state_change_at: datetime | None = None
    last_error_code: str | None = None
    remote_min_search_interval_seconds: int | None = None


class AirDcppSupervisorApi(Protocol):
    async def authorize(self) -> AirDcppAuthenticationInfo: ...

    async def get_current_session(self) -> AirDcppSession: ...

    async def get_system_info(self) -> AirDcppSystemInfo: ...

    async def get_settings(self, keys: list[str]) -> list[str | bool | int]: ...

    async def download_search_result(
        self,
        instance_id: int,
        result_id: str,
        *,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo: ...

    async def get_queue_files_by_tth(self, tth: str) -> list[AirDcppQueueFile]: ...

    async def create_file_bundle(
        self,
        *,
        tth: str,
        size: int,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo: ...

    async def remove_queue_bundle(self, bundle_id: int) -> None: ...

    async def delete_current_session(self) -> None: ...

    async def aclose(self) -> None: ...


class AirDcppSupervisorSocket(Protocol):
    async def connect(self, token: SecretStr) -> None: ...

    async def subscribe(self, path: str, handler: AirDcppEventHandler) -> None: ...

    async def unsubscribe(self, path: str) -> None: ...

    async def wait_disconnected(self) -> None: ...

    async def close(self) -> None: ...


class AirDcppSupervisor:
    """Supervise one reusable REST pool and one WebSocket session."""

    def __init__(
        self,
        *,
        config: AirDcppSupervisorConfig,
        api_client: AirDcppSupervisorApi,
        socket_client: AirDcppSupervisorSocket,
        reconcile: Callable[[int], Awaitable[None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.config = config
        self.api_client = api_client
        self.socket_client = socket_client
        self._reconcile = reconcile or _no_reconciliation
        self._sleep = sleep
        self._jitter = jitter or _default_jitter
        self._runner: asyncio.Task[None] | None = None
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._reconciliation_pending = False
        self._queue_subscriptions_installed = False
        self._state_changed = asyncio.Condition()
        self._health = AirDcppSupervisorHealth(last_state_change_at=_now())
        self._authorized = False
        self._stopping = False

    @property
    def state(self) -> AirDcppSupervisorState:
        return self._health.state

    @property
    def health(self) -> AirDcppSupervisorHealth:
        return self._health

    @property
    def background_task_count(self) -> int:
        return sum(
            task is not None and not task.done()
            for task in (self._runner, self._reconciliation_task)
        )

    def start(self) -> None:
        """Schedule connection work without waiting on the remote service."""
        if not self.config.enabled or self._stopping:
            return
        if self._runner is not None and not self._runner.done():
            return
        self._runner = asyncio.create_task(
            self._run(), name=f"airdcpp-supervisor-{self.config.config_id}"
        )

    async def stop(self) -> None:
        """Cancel and await all owned work, then close both transports."""
        if self._stopping:
            return
        self._stopping = True
        if self.state is not AirDcppSupervisorState.DISABLED:
            await self._set_state(AirDcppSupervisorState.STOPPING)
        runner = self._runner
        if runner is not None and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self._runner = None
        reconciliation_task = self._reconciliation_task
        if reconciliation_task is not None and not reconciliation_task.done():
            reconciliation_task.cancel()
            await asyncio.gather(reconciliation_task, return_exceptions=True)
        self._reconciliation_task = None
        self._reconciliation_pending = False
        await self._end_session()
        with suppress(Exception):
            await self.socket_client.close()
        with suppress(Exception):
            await self.api_client.aclose()
        await self._set_state(AirDcppSupervisorState.DISABLED)

    async def subscribe(self, path: str, handler: AirDcppEventHandler) -> None:
        """Register desired subscriptions before or after supervisor startup."""
        await self.socket_client.subscribe(path, handler)

    async def unsubscribe(self, path: str) -> None:
        await self.socket_client.unsubscribe(path)

    async def wait_for_state(
        self,
        state: AirDcppSupervisorState,
        *,
        timeout_seconds: float,
    ) -> None:
        """Wait deterministically for tests and bounded service coordination."""
        async with asyncio.timeout(timeout_seconds):
            async with self._state_changed:
                await self._state_changed.wait_for(lambda: self.state is state)

    async def _run(self) -> None:
        reconnect_attempt = 0
        try:
            await self._install_queue_subscriptions()
            while not self._stopping:
                await self._set_state(AirDcppSupervisorState.CONNECTING)
                try:
                    auth = await self.api_client.authorize()
                    self._authorized = True
                    session = await self.api_client.get_current_session()
                    system = await self.api_client.get_system_info()
                    self._validate_compatibility(auth, session, system)
                    remote_settings = await self.api_client.get_settings(["min_search_interval"])
                    remote_interval = (
                        remote_settings[0]
                        if len(remote_settings) == 1 and type(remote_settings[0]) is int
                        else None
                    )
                    if remote_interval is None or remote_interval < 45:
                        await self._set_health(
                            state=AirDcppSupervisorState.UNSAFE_SEARCH_INTERVAL,
                            compatible=True,
                            api_version=system.api_version,
                            api_feature_level=system.api_feature_level,
                            remote_min_search_interval_seconds=remote_interval,
                            last_error_code="unsafe_search_interval",
                        )
                        await self._end_session()
                        return
                    await self._set_health(
                        state=AirDcppSupervisorState.COMPATIBLE_REST,
                        compatible=True,
                        api_version=system.api_version,
                        api_feature_level=system.api_feature_level,
                        missing_permissions=(),
                        remote_min_search_interval_seconds=remote_interval,
                        last_error_code=None,
                    )
                except AirDcppAuthenticationError:
                    await self._set_state(
                        AirDcppSupervisorState.AUTHENTICATION_FAILED,
                        error_code="authentication",
                    )
                    return
                except AirDcppPermissionError as exc:
                    missing = (exc.missing_permission,) if exc.missing_permission else ()
                    await self._set_health(
                        state=AirDcppSupervisorState.PERMISSION_FAILED,
                        missing_permissions=missing,
                        last_error_code="permission",
                    )
                    return
                except AirDcppCompatibilityError:
                    await self._set_state(
                        AirDcppSupervisorState.INCOMPATIBLE,
                        error_code="compatibility",
                    )
                    return
                except Exception:
                    reconnect_attempt += 1
                    await self._set_health(
                        state=AirDcppSupervisorState.UNAVAILABLE,
                        reconnect_attempts=reconnect_attempt,
                        last_error_code="unavailable",
                    )
                    await self._end_session()
                    await self._sleep(self._jitter(_backoff_seconds(reconnect_attempt)))
                    continue

                try:
                    await self._set_state(AirDcppSupervisorState.SOCKET_CONNECTING)
                    await self.socket_client.connect(auth.auth_token)
                    reconnect_attempt = 0
                    await self._set_health(
                        state=AirDcppSupervisorState.READY,
                        compatible=True,
                        reconnect_attempts=0,
                        last_ready_at=_now(),
                        last_error_code=None,
                    )
                    with suppress(Exception):
                        await self._reconcile(self.config.config_id)
                    await self.socket_client.wait_disconnected()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    reconnect_attempt += 1
                    await self._set_health(
                        state=AirDcppSupervisorState.DEGRADED_SOCKET,
                        compatible=True,
                        reconnect_attempts=reconnect_attempt,
                        last_error_code="socket_unavailable",
                    )
                    await self._end_session()
                    await self._sleep(self._jitter(_backoff_seconds(reconnect_attempt)))
        finally:
            await self._end_session()

    async def _install_queue_subscriptions(self) -> None:
        if self._queue_subscriptions_installed:
            return
        for path in _QUEUE_LISTENER_PATHS:
            await self.socket_client.subscribe(path, self._queue_event)
        self._queue_subscriptions_installed = True

    async def _queue_event(self, _data: object) -> None:
        """Coalesce queue-event bursts into one bounded REST reconciliation."""
        if self._stopping:
            return
        self._reconciliation_pending = True
        task = self._reconciliation_task
        if task is None or task.done():
            self._reconciliation_task = asyncio.create_task(
                self._drain_reconciliation(),
                name=f"airdcpp-reconcile-{self.config.config_id}",
            )

    async def _drain_reconciliation(self) -> None:
        # Give one event-loop turn to collect a typical AirDC++ tick/status burst.
        await asyncio.sleep(0)
        while self._reconciliation_pending and not self._stopping:
            self._reconciliation_pending = False
            with suppress(Exception):
                await self._reconcile(self.config.config_id)

    def _validate_compatibility(
        self,
        auth: AirDcppAuthenticationInfo,
        session: AirDcppSession,
        system: AirDcppSystemInfo,
    ) -> None:
        if system.api_version != _API_VERSION or system.api_feature_level < _MINIMUM_FEATURE_LEVEL:
            raise AirDcppCompatibilityError("Unsupported AirDC++ API version")
        permissions = frozenset(session.user.permissions)
        # Authorization and session views must agree on the current user.
        permissions &= frozenset(auth.user.permissions)
        missing = sorted(_REQUIRED_PERMISSIONS - permissions)
        if missing:
            raise AirDcppPermissionError(missing[0])

    async def _end_session(self) -> None:
        if not self._authorized:
            return
        self._authorized = False
        with suppress(Exception):
            await self.api_client.delete_current_session()

    async def _set_state(
        self,
        state: AirDcppSupervisorState,
        *,
        error_code: str | None = None,
    ) -> None:
        await self._set_health(state=state, last_error_code=error_code)

    async def _set_health(self, **changes: object) -> None:
        self._health = replace(
            self._health,
            **changes,  # type: ignore[arg-type]
            last_state_change_at=_now(),
        )
        async with self._state_changed:
            self._state_changed.notify_all()


class RegistrySupervisor(Protocol):
    config: AirDcppSupervisorConfig

    @property
    def state(self) -> AirDcppSupervisorState: ...

    @property
    def health(self) -> AirDcppSupervisorHealth: ...

    @property
    def api_client(self) -> AirDcppSupervisorApi: ...

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class AirDcppSupervisorRegistry:
    """Own exactly one supervisor for each enabled exact client config."""

    def __init__(
        self,
        *,
        supervisor_factory: Callable[[AirDcppSupervisorConfig], RegistrySupervisor],
    ) -> None:
        self._supervisor_factory = supervisor_factory
        self._supervisors: dict[int, RegistrySupervisor] = {}
        self._lock = asyncio.Lock()

    @property
    def config_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._supervisors))

    def get(self, config_id: int) -> RegistrySupervisor | None:
        return self._supervisors.get(config_id)

    async def apply(self, configs: Iterable[AirDcppSupervisorConfig]) -> None:
        """Add, replace, or remove only supervisors whose exact config changed."""
        desired = {config.config_id: config for config in configs if config.enabled}
        async with self._lock:
            removed = set(self._supervisors) - set(desired)
            changed = {
                config_id
                for config_id, config in desired.items()
                if config_id in self._supervisors and self._supervisors[config_id].config != config
            }
            for config_id in sorted(removed | changed):
                supervisor = self._supervisors.pop(config_id)
                await supervisor.stop()
            for config_id in sorted(set(desired) - set(self._supervisors)):
                supervisor = self._supervisor_factory(desired[config_id])
                self._supervisors[config_id] = supervisor
                supervisor.start()

    async def stop(self) -> None:
        """Stop all supervisors and clear the registry."""
        async with self._lock:
            supervisors = tuple(self._supervisors.values())
            self._supervisors.clear()
            if supervisors:
                await asyncio.gather(*(supervisor.stop() for supervisor in supervisors))


async def _no_reconciliation(_config_id: int) -> None:
    return None


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff_seconds(attempt: int) -> float:
    return min(30.0, float(2 ** max(0, attempt - 1)))


def _default_jitter(seconds: float) -> float:
    return random.uniform(seconds * 0.8, seconds * 1.2)
