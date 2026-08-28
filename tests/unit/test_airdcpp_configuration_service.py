"""Read-only AirDC++ configuration-test policy tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from pullbox.providers.airdcpp.contracts import (
    AirDcppAuthenticationInfo,
    AirDcppConnectivityInfo,
    AirDcppHub,
    AirDcppSession,
    AirDcppSystemInfo,
)
from pullbox.providers.airdcpp.errors import AirDcppAuthenticationError
from pullbox.services.airdcpp_configuration_service import (
    AirDcppConfigurationService,
    AirDcppConnectionTestStatus,
)

if TYPE_CHECKING:
    from pullbox.providers.airdcpp.contracts import AirDcppQueueBundle


REQUIRED_PERMISSIONS = [
    "search",
    "download",
    "queue_view",
    "queue_edit",
    "hubs_view",
    "settings_view",
]


def _system_info(*, api_version: int = 1, feature_level: int = 10) -> AirDcppSystemInfo:
    return AirDcppSystemInfo(
        api_version=api_version,
        api_feature_level=feature_level,
        client_version="AirDC++w 2.14.0 x86_64",
        platform="linux",
        path_separator="/",
    )


def _session(permissions: list[str]) -> AirDcppSession:
    return AirDcppSession(
        id=123,
        user={"username": "pullbox", "permissions": permissions},
    )


def _connectivity(
    *,
    v4_enabled: bool = True,
    v4_auto: bool = True,
    v4_text: str = "Active mode",
) -> AirDcppConnectivityInfo:
    return AirDcppConnectivityInfo(
        status_v4={
            "auto_detect": v4_auto,
            "enabled": v4_enabled,
            "text": v4_text,
            "bind_address": "0.0.0.0",
            "external_ip": "203.0.113.44",
        },
        status_v6={
            "auto_detect": False,
            "enabled": False,
            "text": "Disabled",
            "bind_address": "::",
            "external_ip": "::",
        },
        tcp_port=21248,
        tls_port=21249,
        udp_port=21248,
    )


class FakeAirDcppClient:
    """Only exposes the read endpoints allowed during a client test."""

    def __init__(
        self,
        *,
        permissions: list[str] | None = None,
        system_info: AirDcppSystemInfo | None = None,
        hubs: list[AirDcppHub] | None = None,
        connectivity: AirDcppConnectivityInfo | None = None,
        minimum_search_interval: int = 45,
        authorize_error: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.permissions = permissions if permissions is not None else REQUIRED_PERMISSIONS
        self.system_info = system_info or _system_info()
        self.hubs = (
            hubs
            if hubs is not None
            else [
                AirDcppHub(
                    id=1,
                    hub_url=SecretStr("adcs://private.example.test:1511"),
                    connect_state={"id": "connected", "str": "Connected"},
                )
            ]
        )
        self.connectivity = connectivity or _connectivity()
        self.minimum_search_interval = minimum_search_interval
        self.authorize_error = authorize_error

    async def authorize(self) -> AirDcppAuthenticationInfo:
        self.calls.append("authorize")
        if self.authorize_error:
            raise self.authorize_error
        return AirDcppAuthenticationInfo(
            session_id=123,
            auth_token=SecretStr("memory-only-token"),
            token_type="Bearer",
            system_info=self.system_info,
            user={"username": "pullbox", "permissions": self.permissions},
            wizard_pending=False,
        )

    async def get_current_session(self) -> AirDcppSession:
        self.calls.append("get_current_session")
        return _session(self.permissions)

    async def get_system_info(self) -> AirDcppSystemInfo:
        self.calls.append("get_system_info")
        return self.system_info

    async def get_hubs(self) -> list[AirDcppHub]:
        self.calls.append("get_hubs")
        return self.hubs

    async def get_connectivity_status(self) -> AirDcppConnectivityInfo:
        self.calls.append("get_connectivity_status")
        return self.connectivity

    async def get_settings(self, keys: list[str]) -> list[str | bool | int]:
        self.calls.append(f"get_settings:{','.join(keys)}")
        return [self.minimum_search_interval]

    async def get_queue_bundles(self, *, start: int, count: int) -> list[AirDcppQueueBundle]:
        self.calls.append(f"get_queue_bundles:{start}:{count}")
        return []

    async def delete_current_session(self) -> None:
        self.calls.append("delete_current_session")

    async def aclose(self) -> None:
        self.calls.append("aclose")


async def test_read_only_connection_test_succeeds_without_transfers_permission() -> None:
    client = FakeAirDcppClient()
    service = AirDcppConfigurationService(lambda: client)

    result = await service.test_connection(
        configured_minimum_search_interval_seconds=45,
        remote_path="/Downloads",
        download_dir="/downloads",
    )

    assert result.status is AirDcppConnectionTestStatus.CONNECTED_WITH_WARNINGS
    assert result.healthy is True
    assert result.compatible is True
    assert result.missing_permissions == ()
    assert result.permissions == tuple(REQUIRED_PERMISSIONS)
    assert result.connected_hub_count == 1
    assert result.queue_accessible is True
    assert result.path_mapping_configured is True
    assert result.websocket_state == "not_tested"
    assert result.warnings == ("websocket_not_tested",)
    assert result.minimum_search_interval_seconds == 45
    assert "transfers" not in result.permissions
    assert client.calls == [
        "authorize",
        "get_current_session",
        "get_system_info",
        "get_hubs",
        "get_connectivity_status",
        "get_settings:min_search_interval",
        "get_queue_bundles:0:1",
        "delete_current_session",
        "aclose",
    ]


async def test_connection_test_reports_safe_warnings_without_sensitive_values() -> None:
    client = FakeAirDcppClient(
        permissions=[*REQUIRED_PERMISSIONS, "admin"],
        hubs=[],
        connectivity=_connectivity(
            v4_enabled=True,
            v4_auto=False,
            v4_text="Passive mode 203.0.113.44",
        ),
    )
    service = AirDcppConfigurationService(lambda: client)

    result = await service.test_connection(
        configured_minimum_search_interval_seconds=45,
        remote_path=None,
        download_dir=None,
    )

    assert result.status is AirDcppConnectionTestStatus.CONNECTED_WITH_WARNINGS
    assert result.healthy is True
    assert result.connectivity_mode_v4 == "passive"
    assert result.connected_hub_count == 0
    assert result.path_mapping_configured is False
    assert {"admin_permission", "no_connected_hubs", "passive_ipv4", "path_mapping"} <= set(
        result.warnings
    )
    rendered = repr(result)
    assert "203.0.113.44" not in rendered
    assert "private.example.test" not in rendered
    assert "memory-only-token" not in rendered


@pytest.mark.parametrize(
    ("permissions", "system_info", "minimum_interval", "expected_message"),
    [
        (
            [permission for permission in REQUIRED_PERMISSIONS if permission != "queue_edit"],
            _system_info(),
            45,
            "missing required permissions",
        ),
        (REQUIRED_PERMISSIONS, _system_info(api_version=2), 45, "API version 1"),
        (REQUIRED_PERMISSIONS, _system_info(feature_level=9), 45, "feature level 10"),
        (REQUIRED_PERMISSIONS, _system_info(), 32, "at least 45 seconds"),
    ],
)
async def test_connection_test_fails_closed_on_permission_compatibility_or_interval(
    permissions: list[str],
    system_info: AirDcppSystemInfo,
    minimum_interval: int,
    expected_message: str,
) -> None:
    client = FakeAirDcppClient(
        permissions=permissions,
        system_info=system_info,
        minimum_search_interval=minimum_interval,
    )
    service = AirDcppConfigurationService(lambda: client)

    result = await service.test_connection(
        configured_minimum_search_interval_seconds=45,
        remote_path="/Downloads",
        download_dir="/downloads",
    )

    assert result.status is AirDcppConnectionTestStatus.NEEDS_ATTENTION
    assert result.healthy is False
    assert expected_message in result.message
    assert client.calls[-2:] == ["delete_current_session", "aclose"]
    if "queue_edit" not in permissions:
        assert result.missing_permissions == ("queue_edit",)
        assert all(not call.startswith("get_hubs") for call in client.calls)


async def test_connection_test_normalizes_errors_and_always_closes_client() -> None:
    client = FakeAirDcppClient(authorize_error=AirDcppAuthenticationError())
    service = AirDcppConfigurationService(lambda: client)

    result = await service.test_connection(
        configured_minimum_search_interval_seconds=45,
        remote_path="/Downloads",
        download_dir="/downloads",
    )

    assert result.status is AirDcppConnectionTestStatus.NEEDS_ATTENTION
    assert result.healthy is False
    assert result.message == "AirDC++ authentication failed"
    assert client.calls == ["authorize", "aclose"]
