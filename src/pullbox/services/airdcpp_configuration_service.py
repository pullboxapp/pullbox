"""Read-only AirDC++ configuration and compatibility checks."""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pullbox.providers.airdcpp.errors import AirDcppCompatibilityError, AirDcppError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.providers.airdcpp.contracts import (
        AirDcppAuthenticationInfo,
        AirDcppConnectivityInfo,
        AirDcppHub,
        AirDcppQueueBundle,
        AirDcppSession,
        AirDcppSystemInfo,
    )

_API_VERSION = 1
_MINIMUM_API_FEATURE_LEVEL = 10
_REQUIRED_PERMISSIONS = (
    "search",
    "download",
    "queue_view",
    "queue_edit",
    "hubs_view",
    "settings_view",
)


class AirDcppConnectionTestStatus(StrEnum):
    """User-facing result states for an AirDC++ client test."""

    CONNECTED = "connected"
    CONNECTED_WITH_WARNINGS = "connected_with_warnings"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class AirDcppConnectionTestResult:
    """Secret-safe diagnostic result from the read-only compatibility test."""

    status: AirDcppConnectionTestStatus
    healthy: bool
    message: str
    response_time_ms: float
    api_version: int | None = None
    api_feature_level: int | None = None
    client_version: str | None = None
    compatible: bool = False
    permissions: tuple[str, ...] = ()
    missing_permissions: tuple[str, ...] = ()
    connected_hub_count: int | None = None
    websocket_state: str = "not_tested"
    connectivity_mode_v4: str | None = None
    tcp_port: int | None = None
    udp_port: int | None = None
    tls_port: int | None = None
    queue_accessible: bool = False
    path_mapping_configured: bool = False
    minimum_search_interval_seconds: int | None = None
    warnings: tuple[str, ...] = ()


class AirDcppReadOnlyClient(Protocol):
    """Narrow transport surface allowed during a configuration test."""

    async def authorize(self) -> AirDcppAuthenticationInfo: ...

    async def get_current_session(self) -> AirDcppSession: ...

    async def get_system_info(self) -> AirDcppSystemInfo: ...

    async def get_hubs(self) -> list[AirDcppHub]: ...

    async def get_connectivity_status(self) -> AirDcppConnectivityInfo: ...

    async def get_settings(self, keys: list[str]) -> list[str | bool | int]: ...

    async def get_queue_bundles(
        self,
        *,
        start: int,
        count: int,
    ) -> list[AirDcppQueueBundle]: ...

    async def delete_current_session(self) -> None: ...

    async def aclose(self) -> None: ...


class AirDcppConfigurationService:
    """Run the supported AirDC++ test without mutating searches or settings."""

    def __init__(self, client_factory: Callable[[], AirDcppReadOnlyClient]) -> None:
        self._client_factory = client_factory

    async def test_connection(
        self,
        *,
        configured_minimum_search_interval_seconds: int,
        remote_path: str | None,
        download_dir: str | None,
    ) -> AirDcppConnectionTestResult:
        """Inspect compatibility, permissions, hubs, connectivity, and queue access."""
        started = time.monotonic()
        client = self._client_factory()
        authorized = False
        path_mapping_configured = bool(remote_path and download_dir)

        try:
            await client.authorize()
            authorized = True
            session = await client.get_current_session()
            system_info = await client.get_system_info()
            permissions = tuple(
                permission
                for permission in (*_REQUIRED_PERMISSIONS, "admin")
                if permission in session.user.permissions
            )
            missing_permissions = tuple(
                permission
                for permission in _REQUIRED_PERMISSIONS
                if permission not in session.user.permissions
            )

            if system_info.api_version != _API_VERSION:
                return self._failure(
                    started,
                    (
                        f"AirDC++ API version 1 is required; "
                        f"server reported {system_info.api_version}"
                    ),
                    system_info=system_info,
                    permissions=permissions,
                    missing_permissions=missing_permissions,
                    path_mapping_configured=path_mapping_configured,
                )
            if system_info.api_feature_level < _MINIMUM_API_FEATURE_LEVEL:
                return self._failure(
                    started,
                    (
                        "AirDC++ API feature level 10 or newer is required; "
                        f"server reported {system_info.api_feature_level}"
                    ),
                    system_info=system_info,
                    permissions=permissions,
                    missing_permissions=missing_permissions,
                    path_mapping_configured=path_mapping_configured,
                )
            if missing_permissions:
                return self._failure(
                    started,
                    "AirDC++ is missing required permissions: " + ", ".join(missing_permissions),
                    system_info=system_info,
                    permissions=permissions,
                    missing_permissions=missing_permissions,
                    path_mapping_configured=path_mapping_configured,
                )

            hubs = await client.get_hubs()
            connectivity = await client.get_connectivity_status()
            setting_values = await client.get_settings(["min_search_interval"])
            minimum_interval = setting_values[0]
            if type(minimum_interval) is not int:
                raise AirDcppCompatibilityError(
                    "AirDC++ returned an incompatible minimum search interval"
                )
            if minimum_interval < configured_minimum_search_interval_seconds:
                return self._failure(
                    started,
                    (
                        "AirDC++ Minimum search interval must be at least "
                        f"{configured_minimum_search_interval_seconds} seconds; "
                        f"server reported {minimum_interval}"
                    ),
                    system_info=system_info,
                    permissions=permissions,
                    path_mapping_configured=path_mapping_configured,
                    minimum_search_interval_seconds=minimum_interval,
                )

            await client.get_queue_bundles(start=0, count=1)
            connected_hub_count = sum(hub.connected for hub in hubs)
            connectivity_mode = _safe_connectivity_mode(connectivity)
            warnings = _warnings(
                permissions=permissions,
                connected_hub_count=connected_hub_count,
                connectivity_mode=connectivity_mode,
                path_mapping_configured=path_mapping_configured,
            )
            status = (
                AirDcppConnectionTestStatus.CONNECTED_WITH_WARNINGS
                if warnings
                else AirDcppConnectionTestStatus.CONNECTED
            )
            message = "Connected with warnings" if warnings else "Connected"
            return AirDcppConnectionTestResult(
                status=status,
                healthy=True,
                message=message,
                response_time_ms=_elapsed_ms(started),
                api_version=system_info.api_version,
                api_feature_level=system_info.api_feature_level,
                client_version=system_info.client_version,
                compatible=True,
                permissions=permissions,
                connected_hub_count=connected_hub_count,
                connectivity_mode_v4=connectivity_mode,
                tcp_port=connectivity.tcp_port,
                udp_port=connectivity.udp_port,
                tls_port=connectivity.tls_port,
                queue_accessible=True,
                path_mapping_configured=path_mapping_configured,
                minimum_search_interval_seconds=minimum_interval,
                warnings=warnings,
            )
        except AirDcppError as exc:
            return AirDcppConnectionTestResult(
                status=AirDcppConnectionTestStatus.NEEDS_ATTENTION,
                healthy=False,
                message=str(exc),
                response_time_ms=_elapsed_ms(started),
                path_mapping_configured=path_mapping_configured,
            )
        finally:
            if authorized:
                with suppress(AirDcppError):
                    await client.delete_current_session()
            await client.aclose()

    @staticmethod
    def _failure(
        started: float,
        message: str,
        *,
        system_info: AirDcppSystemInfo,
        permissions: tuple[str, ...],
        missing_permissions: tuple[str, ...] = (),
        path_mapping_configured: bool,
        minimum_search_interval_seconds: int | None = None,
    ) -> AirDcppConnectionTestResult:
        return AirDcppConnectionTestResult(
            status=AirDcppConnectionTestStatus.NEEDS_ATTENTION,
            healthy=False,
            message=message,
            response_time_ms=_elapsed_ms(started),
            api_version=system_info.api_version,
            api_feature_level=system_info.api_feature_level,
            client_version=system_info.client_version,
            compatible=False,
            permissions=permissions,
            missing_permissions=missing_permissions,
            path_mapping_configured=path_mapping_configured,
            minimum_search_interval_seconds=minimum_search_interval_seconds,
        )


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _safe_connectivity_mode(connectivity: AirDcppConnectivityInfo) -> str:
    status = connectivity.status_v4
    if not status.enabled:
        return "disabled"
    if "passive" in status.text.casefold():
        return "passive"
    return "active_auto" if status.auto_detect else "active_manual"


def _warnings(
    *,
    permissions: tuple[str, ...],
    connected_hub_count: int,
    connectivity_mode: str,
    path_mapping_configured: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if "admin" in permissions:
        warnings.append("admin_permission")
    if connected_hub_count == 0:
        warnings.append("no_connected_hubs")
    if connectivity_mode == "passive":
        warnings.append("passive_ipv4")
    elif connectivity_mode == "disabled":
        warnings.append("disabled_ipv4")
    if not path_mapping_configured:
        warnings.append("path_mapping")
    # R2 deliberately proves only the REST path. The supervised WebSocket
    # session is added and tested in R3, so report that limitation explicitly.
    warnings.append("websocket_not_tested")
    return tuple(warnings)
