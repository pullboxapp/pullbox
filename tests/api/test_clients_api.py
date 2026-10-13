"""Tests for download client API error handling."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError

from pullbox.api.v1 import clients as clients_api
from pullbox.core.encryption import decrypt_secret, encrypt_secret
from pullbox.core.exceptions import NotFoundError, ProviderError
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.providers.base import ProviderHealthResult
from pullbox.schemas.client import ClientCreate, ClientUpdate
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]


async def _instant_sleep(_seconds: float) -> None:
    return None


def _client_payload(
    *,
    name: str = "SABnzbd",
    client_type: str = "sabnzbd",
    url: str = "http://localhost:8080",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "client_type": client_type,
        "url": url,
        "enabled": True,
        "priority": 50,
    }
    payload.update(overrides)
    return payload


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME)
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


@pytest.mark.asyncio
class TestClientCrud:
    async def test_list_orders_by_priority_then_name_and_redacts_secret_values(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            session.add_all(
                [
                    DownloadClientConfig(
                        name="Zulu NZBGet",
                        client_type=DownloadClientType.NZBGET,
                        url="http://localhost:6789",
                        enabled=True,
                        priority=20,
                        username="nzbget",
                        password=encrypt_secret("nzb-password"),
                    ),
                    DownloadClientConfig(
                        name="Alpha SAB",
                        client_type=DownloadClientType.SABNZBD,
                        url="http://localhost:8080",
                        enabled=True,
                        priority=20,
                        api_key=encrypt_secret("sab-key"),
                    ),
                    DownloadClientConfig(
                        name="qBit",
                        client_type=DownloadClientType.QBITTORRENT,
                        url="http://localhost:8081",
                        enabled=False,
                        priority=80,
                        username="admin",
                        password=encrypt_secret("qbit-password"),
                    ),
                ]
            )
            await session.commit()

        resp = await authenticated_client.get("/api/v1/clients")

        assert resp.status_code == 200
        data = resp.json()
        assert [item["name"] for item in data] == ["Alpha SAB", "Zulu NZBGet", "qBit"]
        assert data[0]["has_api_key"] is True
        assert data[0]["has_password"] is False
        assert data[1]["has_api_key"] is False
        assert data[1]["has_password"] is True
        assert data[2]["enabled"] is False
        assert all("api_key" not in item for item in data)
        assert all("password" not in item for item in data)

    async def test_create_encrypts_secret_and_rejects_duplicate_client_type(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        resp = await authenticated_client.post(
            "/api/v1/clients",
            json=_client_payload(name="SAB", api_key="raw-sab-key", category="comics"),
            headers=_csrf_header_for(authenticated_client),
        )

        assert resp.status_code == 201
        created = resp.json()
        assert created["name"] == "SAB"
        assert created["has_api_key"] is True
        assert "api_key" not in created

        async with sec_db() as session:
            row = await session.get(DownloadClientConfig, created["id"])
            assert row is not None
            assert row.api_key is not None
            assert row.api_key != "raw-sab-key"
            assert decrypt_secret(row.api_key) == "raw-sab-key"
            assert row.category == "comics"

        # A second client of the same type is allowed, e.g. one per tracker.
        second = await authenticated_client.post(
            "/api/v1/clients",
            json=_client_payload(name="Second SAB", api_key="other-key"),
            headers=_csrf_header_for(authenticated_client),
        )

        assert second.status_code == 201
        assert second.json()["id"] != created["id"]
        listed = await authenticated_client.get("/api/v1/clients")
        assert sorted(item["name"] for item in listed.json()) == ["SAB", "Second SAB"]

    async def test_update_preserves_blank_password_and_clears_blank_api_key(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            client = DownloadClientConfig(
                name="qBit",
                client_type=DownloadClientType.QBITTORRENT,
                url="http://localhost:8081",
                enabled=True,
                priority=50,
                api_key=encrypt_secret("legacy-key"),
                username="admin",
                password=encrypt_secret("keep-password"),
                category="old",
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            client_id = client.id

        resp = await authenticated_client.put(
            f"/api/v1/clients/{client_id}",
            json={
                "name": "Updated qBit",
                "priority": 10,
                "api_key": "",
                "password": "",
                "category": "new",
            },
            headers=_csrf_header_for(authenticated_client),
        )

        assert resp.status_code == 200
        updated = resp.json()
        assert updated["name"] == "Updated qBit"
        assert updated["priority"] == 10
        assert updated["has_api_key"] is False
        assert updated["has_password"] is True
        assert updated["category"] == "new"

        async with sec_db() as session:
            row = await session.get(DownloadClientConfig, client_id)
            assert row is not None
            assert row.api_key == ""
            assert row.password is not None
            assert decrypt_secret(row.password) == "keep-password"

    async def test_delete_removes_client_and_missing_client_returns_not_found(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            client = DownloadClientConfig(
                name="Delete Me",
                client_type=DownloadClientType.DELUGE,
                url="http://localhost:8112",
                enabled=True,
                priority=50,
                password=encrypt_secret("deluge-password"),
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            client_id = client.id

        delete_resp = await authenticated_client.delete(
            f"/api/v1/clients/{client_id}",
            headers=_csrf_header_for(authenticated_client),
        )

        assert delete_resp.status_code == 204

        get_resp = await authenticated_client.get(f"/api/v1/clients/{client_id}")
        assert get_resp.status_code == 404

        second_delete = await authenticated_client.delete(
            f"/api/v1/clients/{client_id}",
            headers=_csrf_header_for(authenticated_client),
        )
        assert second_delete.status_code == 404


@pytest.mark.asyncio
class TestClientTestConnection:
    async def test_successful_client_test_persists_health_and_message(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async with sec_db() as session:
            client = DownloadClientConfig(
                name="Healthy SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://localhost:8080",
                enabled=True,
                priority=50,
                api_key=encrypt_secret("sab-key"),
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            client_id = client.id

        async def _healthy_test(_self) -> ProviderHealthResult:
            return ProviderHealthResult(
                healthy=True,
                message="SABnzbd 4.5.1",
                response_time_ms=41.0,
            )

        monkeypatch.setattr(
            "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
            _healthy_test,
        )

        resp = await authenticated_client.post(
            f"/api/v1/clients/{client_id}/test",
            headers=_csrf_header_for(authenticated_client),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is True
        assert data["message"] == "SABnzbd 4.5.1"

        async with sec_db() as session:
            refreshed = await session.get(DownloadClientConfig, client_id)
            assert refreshed is not None
            assert refreshed.last_success_at is not None
            assert refreshed.last_test_message == "SABnzbd 4.5.1"
            assert refreshed.last_error is None

    async def test_saved_secret_decrypt_failure_returns_failed_result(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            client = DownloadClientConfig(
                name="Broken SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://localhost:8080",
                enabled=True,
                priority=50,
                api_key="enc:not-a-valid-token",
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            client_id = client.id

        resp = await authenticated_client.post(
            f"/api/v1/clients/{client_id}/test",
            headers=_csrf_header_for(authenticated_client),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is False
        assert "could not be decrypted" in data["message"].lower()

        async with sec_db() as session:
            refreshed = await session.get(DownloadClientConfig, client_id)
            assert refreshed is not None
            assert refreshed.last_failure_at is not None
            assert refreshed.last_error is not None
            assert refreshed.last_test_message is not None
            assert "could not be decrypted" in refreshed.last_error.lower()
            assert refreshed.last_test_message == refreshed.last_error

    async def test_inline_edit_test_reuses_saved_credentials_when_form_secrets_blank(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        constructed: dict[str, object] = {}

        async with sec_db() as session:
            client = DownloadClientConfig(
                name="Existing qBit",
                client_type=DownloadClientType.QBITTORRENT,
                url="http://localhost:8081",
                enabled=True,
                priority=50,
                username="saved-user",
                password=encrypt_secret("saved-password"),
                category="comics",
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            client_id = client.id

        def _capture_init(self, **kwargs: object) -> None:
            constructed.update(kwargs)

        async def _healthy_test(_self) -> ProviderHealthResult:
            return ProviderHealthResult(
                healthy=True,
                message="qBittorrent is reachable",
                response_time_ms=12.5,
            )

        monkeypatch.setattr(
            "pullbox.providers.download.qbittorrent.QBittorrentClient.__init__",
            _capture_init,
        )
        monkeypatch.setattr(
            "pullbox.providers.download.qbittorrent.QBittorrentClient.test_connection",
            _healthy_test,
        )
        monkeypatch.setattr(
            "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
            _healthy_test,
        )

        resp = await authenticated_client.post(
            "/api/v1/clients/test",
            params={"existing_id": client_id},
            json=_client_payload(
                name="Existing qBit",
                client_type="qbittorrent",
                url="http://localhost:8081",
                username="",
                password="",
                category="comics",
            ),
            headers=_csrf_header_for(authenticated_client),
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "healthy": True,
            "message": "qBittorrent is reachable",
            "response_time_ms": 12.5,
        }
        assert constructed["username"] == "saved-user"
        assert constructed["password"] == "saved-password"
        assert constructed["category"] == "comics"


def _create_model(**overrides: object) -> ClientCreate:
    return ClientCreate.model_validate(_client_payload(**overrides))


def _health_result(message: str = "reachable", *, healthy: bool = True) -> ProviderHealthResult:
    return ProviderHealthResult(
        healthy=healthy,
        message=message,
        response_time_ms=12.5,
    )


@pytest.mark.asyncio
class TestClientRouteFunctions:
    async def test_crud_route_functions_redact_encrypt_and_validate(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            session.add_all(
                [
                    DownloadClientConfig(
                        name="Zulu NZBGet",
                        client_type=DownloadClientType.NZBGET,
                        url="http://localhost:6789",
                        enabled=True,
                        priority=20,
                        username="nzbget",
                        password=encrypt_secret("nzb-password"),
                    ),
                    DownloadClientConfig(
                        name="Alpha SAB",
                        client_type=DownloadClientType.SABNZBD,
                        url="http://localhost:8080",
                        enabled=True,
                        priority=20,
                        api_key=encrypt_secret("sab-key"),
                    ),
                ]
            )
            await session.flush()

            listed = await clients_api.list_clients(object(), session)  # type: ignore[arg-type]
            assert [client.name for client in listed] == ["Alpha SAB", "Zulu NZBGet"]
            assert listed[0].has_api_key is True
            assert listed[0].has_password is False
            assert listed[1].has_password is True

            fetched = await clients_api.get_client(listed[0].id, object(), session)  # type: ignore[arg-type]
            assert fetched.name == "Alpha SAB"

            created = await clients_api.add_client(
                _create_model(
                    name="qBit",
                    client_type="qbittorrent",
                    url="http://localhost:8081",
                    username="admin",
                    password="raw-password",
                    category="comics",
                ),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert created.has_password is True
            stored = await session.get(DownloadClientConfig, created.id)
            assert stored is not None
            assert stored.password is not None
            assert stored.password != "raw-password"
            assert decrypt_secret(stored.password) == "raw-password"

            second = await clients_api.add_client(
                _create_model(name="Second qBit", client_type="qbittorrent"),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert second.id != created.id

            updated = await clients_api.update_client(
                created.id,
                ClientUpdate(name="Updated qBit", api_key="", password="", priority=10),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert updated.name == "Updated qBit"
            assert updated.priority == 10
            assert updated.has_api_key is False
            assert updated.has_password is True
            await session.refresh(stored)
            assert stored.api_key == ""
            assert stored.password is not None
            assert decrypt_secret(stored.password) == "raw-password"

            updated_secret = await clients_api.update_client(
                created.id,
                ClientUpdate(api_key="new-api-key", password="new-password"),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert updated_secret.has_api_key is True
            assert updated_secret.has_password is True
            await session.refresh(stored)
            assert stored.api_key is not None
            assert stored.password is not None
            assert decrypt_secret(stored.api_key) == "new-api-key"
            assert decrypt_secret(stored.password) == "new-password"

            await clients_api.delete_client(created.id, object(), session)  # type: ignore[arg-type]
            await session.flush()
            assert await session.get(DownloadClientConfig, created.id) is None

            with pytest.raises(NotFoundError):
                await clients_api.get_client(999_001, object(), session)  # type: ignore[arg-type]
            with pytest.raises(NotFoundError):
                await clients_api.update_client(
                    999_002,
                    ClientUpdate(name="Missing"),
                    object(),  # type: ignore[arg-type]
                    session,
                )
            with pytest.raises(NotFoundError):
                await clients_api.delete_client(999_003, object(), session)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("client_type", "patch_target", "payload_overrides"),
        [
            (
                "sabnzbd",
                "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
                {"api_key": "sab-key", "category": "comics"},
            ),
            (
                "nzbget",
                "pullbox.providers.download.nzbget.NZBGetClient.test_connection",
                {"username": "", "password": "nzb-password", "category": "comics"},
            ),
            (
                "qbittorrent",
                "pullbox.providers.download.qbittorrent.QBittorrentClient.test_connection",
                {"username": "admin", "password": "qbit-password", "category": "comics"},
            ),
            (
                "transmission",
                "pullbox.providers.download.transmission.TransmissionClient.test_connection",
                {"username": "admin", "password": "transmission-password"},
            ),
            (
                "deluge",
                "pullbox.providers.download.deluge.DelugeClient.test_connection",
                {"password": "deluge-password"},
            ),
        ],
    )
    async def test_inline_test_route_builds_each_client_type(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        client_type: str,
        patch_target: str,
        payload_overrides: dict[str, object],
    ) -> None:
        async def _healthy_test(_self) -> ProviderHealthResult:
            return _health_result(f"{client_type} reachable")

        monkeypatch.setattr(patch_target, _healthy_test)

        async with sec_db() as session:
            result = await clients_api.test_client_inline(
                _create_model(
                    name=f"{client_type} inline",
                    client_type=client_type,
                    **payload_overrides,
                ),
                object(),  # type: ignore[arg-type]
                session,
            )

        assert result == {
            "healthy": True,
            "message": f"{client_type} reachable",
            "response_time_ms": 12.5,
        }

    async def test_inline_edit_route_reuses_saved_credentials_and_handles_decrypt_failure(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        constructed: dict[str, object] = {}

        def _capture_init(self, **kwargs: object) -> None:
            constructed.update(kwargs)

        async def _healthy_test(_self) -> ProviderHealthResult:
            return _health_result("inline edit reachable")

        monkeypatch.setattr(
            "pullbox.providers.download.qbittorrent.QBittorrentClient.__init__",
            _capture_init,
        )
        monkeypatch.setattr(
            "pullbox.providers.download.qbittorrent.QBittorrentClient.test_connection",
            _healthy_test,
        )
        monkeypatch.setattr(
            "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
            _healthy_test,
        )

        async with sec_db() as session:
            saved = DownloadClientConfig(
                name="Existing qBit",
                client_type=DownloadClientType.QBITTORRENT,
                url="http://localhost:8081",
                enabled=True,
                priority=50,
                username="saved-user",
                password=encrypt_secret("saved-password"),
                category="comics",
            )
            broken = DownloadClientConfig(
                name="Broken qBit",
                client_type=DownloadClientType.QBITTORRENT,
                url="http://localhost:8082",
                enabled=True,
                priority=51,
                password="enc:not-a-valid-token",
            )
            session.add_all([saved, broken])
            await session.flush()

            result = await clients_api.test_client_inline(
                _create_model(
                    name="Existing qBit",
                    client_type="qbittorrent",
                    username="",
                    password="",
                    category="comics",
                ),
                object(),  # type: ignore[arg-type]
                session,
                existing_id=saved.id,
            )
            decrypt_result = await clients_api.test_client_inline(
                _create_model(
                    name="Broken qBit",
                    client_type="qbittorrent",
                    username="",
                    password="",
                ),
                object(),  # type: ignore[arg-type]
                session,
                existing_id=broken.id,
            )

            saved_sab = DownloadClientConfig(
                name="Existing SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://localhost:8083",
                enabled=True,
                priority=52,
                api_key=encrypt_secret("saved-sab-key"),
            )
            session.add(saved_sab)
            await session.flush()
            sab_result = await clients_api.test_client_inline(
                _create_model(name="Existing SAB", client_type="sabnzbd", api_key=""),
                object(),  # type: ignore[arg-type]
                session,
                existing_id=saved_sab.id,
            )

        assert result["healthy"] is True
        assert constructed["username"] == "saved-user"
        assert constructed["password"] == "saved-password"
        assert decrypt_result["healthy"] is False
        assert "could not be decrypted" in str(decrypt_result["message"]).lower()
        assert sab_result["healthy"] is True

    async def test_inline_test_route_rejects_unknown_defensive_client_type(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        body = _create_model(name="Unknown", client_type="sabnzbd").model_copy(
            update={"client_type": "rtorrent"}
        )

        async with sec_db() as session:
            with pytest.raises(ProviderError, match="Unknown client type: rtorrent"):
                await clients_api.test_client_inline(
                    body,
                    object(),  # type: ignore[arg-type]
                    session,
                )

    @pytest.mark.parametrize(
        ("client_type", "patch_target", "config_kwargs"),
        [
            (
                DownloadClientType.SABNZBD,
                "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
                {"api_key": encrypt_secret("sab-key"), "category": "comics"},
            ),
            (
                DownloadClientType.NZBGET,
                "pullbox.providers.download.nzbget.NZBGetClient.test_connection",
                {
                    "username": "nzbget",
                    "password": encrypt_secret("nzb-password"),
                    "category": "comics",
                    "nzbget_priority": "normal",
                    "nzbget_post_processing": "pp3",
                },
            ),
            (
                DownloadClientType.QBITTORRENT,
                "pullbox.providers.download.qbittorrent.QBittorrentClient.test_connection",
                {
                    "username": "admin",
                    "password": encrypt_secret("qbit-password"),
                    "category": "comics",
                    "qbt_content_layout": "Original",
                    "qbt_ratio_limit": 1.5,
                    "qbt_seeding_time_limit": 60,
                },
            ),
            (
                DownloadClientType.TRANSMISSION,
                "pullbox.providers.download.transmission.TransmissionClient.test_connection",
                {
                    "username": "admin",
                    "password": encrypt_secret("transmission-password"),
                    "transmission_download_dir": "/downloads/comics",
                    "transmission_bandwidth_priority": 1,
                    "transmission_seed_ratio_limit": 2.0,
                    "transmission_seed_idle_limit": 120,
                },
            ),
            (
                DownloadClientType.DELUGE,
                "pullbox.providers.download.deluge.DelugeClient.test_connection",
                {
                    "password": encrypt_secret("deluge-password"),
                    "deluge_label": "comics",
                    "deluge_max_ratio": 1.0,
                    "deluge_move_completed_path": "/downloads/complete",
                },
            ),
        ],
    )
    async def test_saved_client_test_route_builds_each_client_type_and_persists_success(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        client_type: DownloadClientType,
        patch_target: str,
        config_kwargs: dict[str, object],
    ) -> None:
        async def _healthy_test(_self) -> ProviderHealthResult:
            return _health_result(f"{client_type.value} healthy")

        monkeypatch.setattr(patch_target, _healthy_test)

        async with sec_db() as session:
            client = DownloadClientConfig(
                name=f"{client_type.value} saved",
                client_type=client_type,
                url="http://localhost:8080",
                enabled=True,
                priority=50,
                **config_kwargs,
            )
            session.add(client)
            await session.flush()

            result = await clients_api.test_client(client.id, object(), session)  # type: ignore[arg-type]
            await session.refresh(client)

        assert result == {
            "healthy": True,
            "message": f"{client_type.value} healthy",
            "response_time_ms": 12.5,
        }
        assert client.last_success_at is not None
        assert client.last_error is None
        assert client.last_test_message == f"{client_type.value} healthy"

    async def test_saved_client_test_route_retries_transient_sqlite_lock(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _healthy_test(_self) -> ProviderHealthResult:
            return _health_result("qBittorrent reachable after retry")

        monkeypatch.setattr(
            "pullbox.providers.download.qbittorrent.QBittorrentClient.test_connection",
            _healthy_test,
        )

        async with sec_db() as session:
            client = DownloadClientConfig(
                name="qBittorrent saved",
                client_type=DownloadClientType.QBITTORRENT,
                url="http://localhost:8080",
                enabled=True,
                priority=50,
                username="admin",
                password=encrypt_secret("qbit-password"),
            )
            session.add(client)
            await session.commit()
            client_id = client.id
            original_flush = session.flush
            flush_attempts = 0

            async def _flaky_flush(*args: object, **kwargs: object) -> None:
                nonlocal flush_attempts
                flush_attempts += 1
                if flush_attempts == 1:
                    raise OperationalError(
                        "UPDATE download_client_configs",
                        {},
                        sqlite3.OperationalError("database is locked"),
                    )
                await original_flush(*args, **kwargs)

            monkeypatch.setattr(session, "flush", _flaky_flush)
            monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

            result = await clients_api.test_client(client_id, object(), session)  # type: ignore[arg-type]
            refreshed = await session.get(DownloadClientConfig, client_id)

        assert flush_attempts == 2
        assert result["healthy"] is True
        assert refreshed is not None
        assert refreshed.last_success_at is not None
        assert refreshed.last_error is None
        assert refreshed.last_test_message == "qBittorrent reachable after retry"

    async def test_saved_client_test_route_persists_failure_and_handles_errors(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _failed_test(_self) -> ProviderHealthResult:
            return _health_result("client refused connection", healthy=False)

        monkeypatch.setattr(
            "pullbox.providers.download.sabnzbd.SABnzbdClient.test_connection",
            _failed_test,
        )

        async with sec_db() as session:
            failing = DownloadClientConfig(
                name="Failing SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://localhost:8080",
                enabled=True,
                priority=50,
                api_key=encrypt_secret("sab-key"),
            )
            broken = DownloadClientConfig(
                name="Broken SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://localhost:8081",
                enabled=True,
                priority=51,
                api_key="enc:not-a-valid-token",
            )
            session.add_all([failing, broken])
            await session.flush()

            failed_result = await clients_api.test_client(failing.id, object(), session)  # type: ignore[arg-type]
            await session.refresh(failing)
            decrypt_result = await clients_api.test_client(broken.id, object(), session)  # type: ignore[arg-type]
            await session.refresh(broken)

            with pytest.raises(NotFoundError):
                await clients_api.test_client(999_004, object(), session)  # type: ignore[arg-type]

        assert failed_result["healthy"] is False
        assert failing.last_failure_at is not None
        assert failing.last_error == "client refused connection"
        assert decrypt_result["healthy"] is False
        assert broken.last_failure_at is not None
        assert "could not be decrypted" in str(broken.last_error).lower()

    async def test_saved_client_test_route_rejects_unknown_defensive_client_type(self) -> None:
        class _Session:
            async def get(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    client_type="rtorrent",
                    url="http://localhost:9999",
                    category=None,
                )

        with pytest.raises(ProviderError, match="Unknown client type: rtorrent"):
            await clients_api.test_client(
                123,
                object(),  # type: ignore[arg-type]
                _Session(),  # type: ignore[arg-type]
            )
