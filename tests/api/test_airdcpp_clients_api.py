"""AirDC++ download-client configuration API tests."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.api.v1 import clients as clients_api
from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.encryption import decrypt_secret
from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.schemas.client import ClientCreate
from pullbox.services.airdcpp_configuration_service import (
    AirDcppConnectionTestResult,
    AirDcppConnectionTestStatus,
)
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

pytest_plugins = ["conftest_security"]


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME)
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


def _airdcpp_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Local AirDC++",
        "client_type": "airdcpp",
        "url": "http://airdcpp:5600/",
        "enabled": True,
        "priority": 40,
        "username": "pullbox",
        "password": "local-password",
        "remote_path": "/Downloads",
        "download_dir": "/downloads/airdcpp",
        "airdcpp": {
            "minimum_search_interval_seconds": 45,
            "hub_allowlist": [
                "ADCS://HUB.EXAMPLE.TEST:1511/",
                "adcs://hub.example.test:1511",
            ],
        },
    }
    payload.update(overrides)
    return payload


def test_airdcpp_create_schema_requires_safe_connection_and_path_fields() -> None:
    parsed = ClientCreate.model_validate(_airdcpp_payload())

    assert parsed.url == "http://airdcpp:5600"
    assert parsed.airdcpp is not None
    assert parsed.airdcpp.hub_allowlist == ["adcs://hub.example.test:1511"]

    for field in ("username", "remote_path", "download_dir"):
        with pytest.raises(PydanticValidationError):
            ClientCreate.model_validate(_airdcpp_payload(**{field: None}))

    with pytest.raises(PydanticValidationError):
        ClientCreate.model_validate(_airdcpp_payload(remote_path="Downloads"))
    with pytest.raises(PydanticValidationError):
        ClientCreate.model_validate(_airdcpp_payload(download_dir="downloads/airdcpp"))
    with pytest.raises(PydanticValidationError):
        ClientCreate.model_validate(_airdcpp_payload(category="comics"))
    with pytest.raises(PydanticValidationError):
        ClientCreate.model_validate(_airdcpp_payload(url="http://airdcpp:5600/?token=secret"))
    with pytest.raises(PydanticValidationError):
        ClientCreate.model_validate(
            {
                "name": "SAB",
                "client_type": "sabnzbd",
                "url": "http://sabnzbd:8080",
                "airdcpp": {},
            }
        )


@pytest.mark.asyncio
async def test_airdcpp_create_update_list_and_disable_preserve_encrypted_configuration(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )

    create_response = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )

    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["client_type"] == "airdcpp"
    assert created["has_password"] is True
    assert "password" not in created
    assert created["airdcpp"]["minimum_search_interval_seconds"] == 45
    assert created["airdcpp"]["hub_allowlist"] == ["adcs://hub.example.test:1511"]
    client_id = created["id"]

    async with sec_db() as session:
        stored = await session.get(DownloadClientConfig, client_id)
        assert stored is not None
        assert stored.password is not None
        encrypted_password = stored.password
        assert encrypted_password != "local-password"
        assert decrypt_secret(encrypted_password) == "local-password"
        extension = (
            await session.execute(
                select(AirDcppClientSettings).where(
                    AirDcppClientSettings.client_config_id == client_id
                )
            )
        ).scalar_one()
        assert extension.minimum_search_interval_seconds == 45

    update_response = await authenticated_client.put(
        f"/api/v1/clients/{client_id}",
        json={
            "enabled": False,
            "password": "",
            "airdcpp": {
                "minimum_search_interval_seconds": 60,
                "max_results": 300,
                "max_retained_routes": 500,
            },
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert update_response.status_code == 200, update_response.text
    updated = update_response.json()
    assert updated["enabled"] is False
    assert updated["has_password"] is True
    assert updated["airdcpp"]["minimum_search_interval_seconds"] == 60
    assert updated["airdcpp"]["max_results"] == 300

    list_response = await authenticated_client.get("/api/v1/clients")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert listed[0]["airdcpp"]["next_search_allowed_at"] is None
    assert "local-password" not in list_response.text

    async with sec_db() as session:
        stored = await session.get(DownloadClientConfig, client_id)
        assert stored is not None
        assert stored.password == encrypted_password
        extension = (
            await session.execute(
                select(AirDcppClientSettings).where(
                    AirDcppClientSettings.client_config_id == client_id
                )
            )
        ).scalar_one()
        assert extension.minimum_search_interval_seconds == 60
        assert extension.next_search_allowed_at is None


@pytest.mark.asyncio
async def test_multiple_airdcpp_clients_can_be_enabled_independently(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )

    first = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(name="Primary AirDC++"),
        headers=_csrf_header_for(authenticated_client),
    )
    second = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(
            name="Secondary AirDC++",
            url="http://airdcpp-secondary:5600/",
            airdcpp={
                "minimum_search_interval_seconds": 45,
                "hub_allowlist": [],
            },
        ),
        headers=_csrf_header_for(authenticated_client),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    listed = (await authenticated_client.get("/api/v1/clients")).json()
    air_clients = [item for item in listed if item["client_type"] == "airdcpp"]
    assert [item["name"] for item in air_clients] == [
        "Primary AirDC++",
        "Secondary AirDC++",
    ]
    assert all(item["enabled"] is True for item in air_clients)


@pytest.mark.asyncio
async def test_airdcpp_mutations_refresh_runtime_registry_after_commit(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )
    observed: list[tuple[tuple[int, bool], ...]] = []

    async def _observe_committed_configuration(_session: AsyncSession) -> None:
        async with sec_db() as observer:
            rows = (
                await observer.execute(
                    select(DownloadClientConfig)
                    .where(DownloadClientConfig.client_type == DownloadClientType.AIRDCPP)
                    .order_by(DownloadClientConfig.id)
                )
            ).scalars()
            observed.append(tuple((row.id, row.enabled) for row in rows))

    monkeypatch.setattr(
        clients_api,
        "refresh_airdcpp_supervisor_registry_from_session",
        _observe_committed_configuration,
        raising=False,
    )

    created = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    updated = await authenticated_client.put(
        f"/api/v1/clients/{client_id}",
        json={"enabled": False},
        headers=_csrf_header_for(authenticated_client),
    )
    assert updated.status_code == 200, updated.text

    deleted = await authenticated_client.delete(
        f"/api/v1/clients/{client_id}",
        headers=_csrf_header_for(authenticated_client),
    )
    assert deleted.status_code == 204, deleted.text
    assert observed == [((client_id, True),), ((client_id, False),), ()]


@pytest.mark.asyncio
async def test_airdcpp_create_requires_password_and_default_off_flag_blocks_activation(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )
    missing_password = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(password=""),
        headers=_csrf_header_for(authenticated_client),
    )
    assert missing_password.status_code == 422
    assert "password" in missing_password.text.lower()

    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=False),
    )
    disabled = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )
    assert disabled.status_code == 422
    assert "disabled" in disabled.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_state",
    [DownloadState.DOWNLOADING, DownloadState.COMPLETED],
)
async def test_airdcpp_delete_rejects_active_exact_client_history(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    active_state: DownloadState,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )
    created = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )
    client_id = created.json()["id"]

    async with sec_db() as session:
        series = Series(title="Example", sort_title="example", year_start=2026)
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=1.0)
        session.add(issue)
        await session.flush()
        session.add(
            DownloadHistory(
                issue_id=issue.id,
                title="Example 001",
                download_url="dc://opaque",
                download_client=DownloadClientType.AIRDCPP,
                protocol=AcquisitionProtocol.DC,
                download_client_config_id=client_id,
                state=active_state,
            )
        )
        await session.commit()

    blocked = await authenticated_client.delete(
        f"/api/v1/clients/{client_id}",
        headers=_csrf_header_for(authenticated_client),
    )
    assert blocked.status_code == 422
    assert "active" in blocked.text.lower()

    async with sec_db() as session:
        row = await session.get(DownloadClientConfig, client_id)
        assert row is not None


@pytest.mark.asyncio
async def test_saved_airdcpp_test_decrypts_password_and_returns_safe_diagnostics(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )
    created = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )
    client_id = created.json()["id"]
    captured: dict[str, object] = {}
    rollback_seen_before_network = False
    original_rollback = AsyncSession.rollback

    async def _track_rollback(session: AsyncSession) -> None:
        nonlocal rollback_seen_before_network
        rollback_seen_before_network = True
        await original_rollback(session)

    monkeypatch.setattr(AsyncSession, "rollback", _track_rollback)

    async def _fake_test(**kwargs: object) -> AirDcppConnectionTestResult:
        assert rollback_seen_before_network is True
        captured.update(kwargs)
        return AirDcppConnectionTestResult(
            status=AirDcppConnectionTestStatus.CONNECTED_WITH_WARNINGS,
            healthy=True,
            message="Connected with warnings",
            response_time_ms=12.5,
            api_version=1,
            api_feature_level=10,
            client_version="AirDC++w 2.14.0 x86_64",
            compatible=True,
            permissions=(
                "search",
                "download",
                "queue_view",
                "queue_edit",
                "hubs_view",
                "settings_view",
            ),
            connected_hub_count=0,
            connectivity_mode_v4="active_auto",
            tcp_port=21248,
            udp_port=21248,
            tls_port=21249,
            queue_accessible=True,
            path_mapping_configured=True,
            minimum_search_interval_seconds=45,
            warnings=("no_connected_hubs",),
        )

    monkeypatch.setattr(clients_api, "_run_airdcpp_connection_test", _fake_test)

    response = await authenticated_client.post(
        f"/api/v1/clients/{client_id}/test",
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "connected_with_warnings"
    assert result["healthy"] is True
    assert result["api_version"] == 1
    assert result["api_feature_level"] == 10
    assert result["connected_hub_count"] == 0
    assert result["queue_accessible"] is True
    assert result["warnings"] == ["no_connected_hubs"]
    assert "local-password" not in response.text
    assert captured == {
        "url": "http://airdcpp:5600",
        "username": "pullbox",
        "password": "local-password",
        "minimum_search_interval_seconds": 45,
        "request_timeout_seconds": 15,
        "remote_path": "/Downloads",
        "download_dir": "/downloads/airdcpp",
    }

    async with sec_db() as session:
        stored = await session.get(DownloadClientConfig, client_id)
        assert stored is not None
        assert stored.last_success_at is not None
        assert stored.last_test_message == "Connected with warnings"
        assert stored.last_error is None


@pytest.mark.asyncio
async def test_airdcpp_test_route_is_unreachable_when_feature_flag_is_off(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=True),
    )
    created = await authenticated_client.post(
        "/api/v1/clients",
        json=_airdcpp_payload(),
        headers=_csrf_header_for(authenticated_client),
    )
    client_id = created.json()["id"]

    monkeypatch.setattr(
        clients_api,
        "get_settings",
        lambda: SimpleNamespace(airdcpp_enabled=False),
    )
    called = False

    async def _must_not_call(**_kwargs: object) -> AirDcppConnectionTestResult:
        nonlocal called
        called = True
        raise AssertionError("AirDC++ transport must remain unreachable")

    monkeypatch.setattr(clients_api, "_run_airdcpp_connection_test", _must_not_call)

    response = await authenticated_client.post(
        f"/api/v1/clients/{client_id}/test",
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "disabled" in response.text.lower()
    assert called is False
