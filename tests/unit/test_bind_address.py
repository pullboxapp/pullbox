"""Tests for runtime-managed bind address and port configuration.

Verifies:
- Valid IPv4 addresses accepted
- Valid port range accepted (1024-65535)
- SystemConfig defaults populated
- Config API no longer edits bind settings

Run:
    pytest tests/unit/test_bind_address.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.config import DEFAULT_SYSTEM_CONFIG
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-bind")
# Ensure config.xml writes go to a writable temp dir (not /data on CI)
os.environ.setdefault("PULLBOX_DATA_DIR", tempfile.mkdtemp())


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _session_token(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    from pullbox.models.user import User

    async with _db_factory() as session:
        user = User(
            username="binduser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return AuthService.create_session_token(user.id, user.session_version)


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _session_token: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    app.state.db_session_factory = _db_factory

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: _session_token},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Unit: defaults ───────────────────────────────────────────────────


class TestBindDefaults:
    """Verify bind_address and port are runtime-managed bootstrap settings."""

    def test_bind_address_not_in_db_defaults(self) -> None:
        assert "bind_address" not in DEFAULT_SYSTEM_CONFIG

    def test_port_not_in_db_defaults(self) -> None:
        assert "port" not in DEFAULT_SYSTEM_CONFIG

    def test_host_keys_in_config_xml(self) -> None:
        from pullbox.core.config_file import CONFIG_XML_KEYS

        assert "bind_address" not in CONFIG_XML_KEYS
        assert "port" not in CONFIG_XML_KEYS
        assert "secret_key" in CONFIG_XML_KEYS


# ── API: save and retrieve ───────────────────────────────────────────


class TestBindConfigAPI:
    """Runtime network settings are read-only from the generic config API."""

    @pytest.mark.asyncio
    async def test_bind_address_rejected(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"bind_address": "127.0.0.1"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_port_rejected(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"port": "9090"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)


class TestHttpsConfigAPI:
    """HTTPS settings are DB-backed, validated, and restart-required."""

    @pytest.mark.asyncio
    async def test_https_defaults_are_visible(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/config")

        assert resp.status_code == 200
        values = {entry["key"]: entry["value"] for entry in resp.json()}
        assert values["https_enabled"] == "false"
        assert values["https_cert_path"] == ""
        assert values["https_key_path"] == ""

    @pytest.mark.asyncio
    async def test_https_enabled_requires_cert_and_key_paths(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"https_enabled": "true"}},
            headers=_csrf_header_for(client),
        )

        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_https_rejects_paths_outside_cert_root(
        self,
        client: AsyncClient,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cert_root = tmp_path / "certs"
        outside = tmp_path / "outside"
        cert_root.mkdir()
        outside.mkdir()
        cert = outside / "server.crt"
        key = cert_root / "server.key"
        cert.write_text("cert")
        key.write_text("key")
        monkeypatch.setattr(
            "pullbox.api.v1.config.get_runtime_settings",
            lambda: SimpleNamespace(https_cert_root=cert_root),
        )

        resp = await client.put(
            "/api/v1/config",
            json={
                "values": {
                    "https_enabled": "true",
                    "https_cert_path": str(cert),
                    "https_key_path": str(key),
                }
            },
            headers=_csrf_header_for(client),
        )

        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_https_rejects_invalid_cert_key_pair(
        self,
        client: AsyncClient,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cert_root = tmp_path / "certs"
        cert_root.mkdir()
        cert = cert_root / "server.crt"
        key = cert_root / "server.key"
        cert.write_text("cert")
        key.write_text("key")
        monkeypatch.setattr(
            "pullbox.api.v1.config.get_runtime_settings",
            lambda: SimpleNamespace(https_cert_root=cert_root),
        )

        def fail_load_cert_chain(certfile: str, keyfile: str) -> None:
            raise ValueError("bad cert pair")

        monkeypatch.setattr(
            "pullbox.core.https_runtime._load_cert_chain",
            fail_load_cert_chain,
        )

        resp = await client.put(
            "/api/v1/config",
            json={
                "values": {
                    "https_enabled": "true",
                    "https_cert_path": str(cert),
                    "https_key_path": str(key),
                }
            },
            headers=_csrf_header_for(client),
        )

        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_https_save_returns_restart_required(
        self,
        client: AsyncClient,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cert_root = tmp_path / "certs"
        cert_root.mkdir()
        cert = cert_root / "server.crt"
        key = cert_root / "server.key"
        cert.write_text("cert")
        key.write_text("key")
        monkeypatch.setattr(
            "pullbox.api.v1.config.get_runtime_settings",
            lambda: SimpleNamespace(https_cert_root=cert_root),
        )
        monkeypatch.setattr(
            "pullbox.core.https_runtime._load_cert_chain",
            lambda certfile, keyfile: None,
        )

        resp = await client.put(
            "/api/v1/config",
            json={
                "values": {
                    "https_enabled": "true",
                    "https_cert_path": str(cert),
                    "https_key_path": str(key),
                }
            },
            headers=_csrf_header_for(client),
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["restart_required"] is True
        assert set(payload["restart_required_keys"]) == {
            "https_cert_path",
            "https_enabled",
            "https_key_path",
        }

    @pytest.mark.asyncio
    async def test_https_runtime_managed_keys_cannot_be_saved(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "pullbox.api.v1.config.https_runtime_config_values",
            lambda: {"https_enabled": "true"},
        )

        resp = await client.put(
            "/api/v1/config",
            json={"values": {"https_enabled": "false"}},
            headers=_csrf_header_for(client),
        )

        assert resp.status_code in (400, 422)
        assert "runtime-managed" in resp.text


# ── Config enum validation ───────────────────────────────────────────


class TestPreferredFormatValidation:
    """PUT /api/v1/config validates preferred_format against allowed values."""

    @pytest.mark.asyncio
    async def test_valid_format_accepted(self, client: AsyncClient) -> None:
        for fmt in ("cbz", "cbr", "cb7", "pdf", "epub"):
            resp = await client.put(
                "/api/v1/config",
                json={"values": {"preferred_format": fmt}},
                headers=_csrf_header_for(client),
            )
            assert resp.status_code == 200, f"Expected 200 for {fmt}, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_invalid_format_rejected(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"preferred_format": "docx"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)


# ── PullboxSettings: bind_address field ─────────────────────────────


class TestBindAddressInSettings:
    """Verify bind_address exists in PullboxSettings for env var detection."""

    def test_bind_address_field_exists(self) -> None:
        from pullbox.config import PullboxSettings

        assert "bind_address" in PullboxSettings.model_fields

    def test_bind_address_default(self) -> None:
        from pullbox.config import PullboxSettings

        assert PullboxSettings.model_fields["bind_address"].default == "0.0.0.0"

    def test_bind_address_env_var_name(self) -> None:
        """PULLBOX_BIND_ADDRESS should be the recognized env var."""
        from pullbox.config import PullboxSettings

        # pydantic-settings uses env_prefix + field_name
        assert PullboxSettings.model_config.get("env_prefix") == "PULLBOX_"


# ── UI: env_overrides dict ──────────────────────────────────────────


class TestBindAddressGeneralSettings:
    """Verify General settings shows runtime network as read-only status."""

    @pytest.mark.asyncio
    async def test_runtime_network_section_rendered(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=general")
        assert resp.status_code == 200
        assert "Runtime network" in resp.text
        assert "Runtime-managed at startup." in resp.text
        assert "Bind Address" in resp.text
        assert "Port Number" in resp.text

    @pytest.mark.asyncio
    async def test_https_env_values_render_as_runtime_managed(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        runtime = SimpleNamespace(
            bind_address="0.0.0.0",
            port=8585,
            data_dir=tmp_path / "data",
            library_root=tmp_path / "comics",
            covers_dir=tmp_path / "comics" / ".covers",
            logs_dir=tmp_path / "data" / "logs",
            temp_dir=tmp_path / "data" / "tmp",
            backup_dir=tmp_path / "data" / "backups",
            https_enabled=True,
            https_cert_path="/config/certs/env.crt",
            https_key_path="/config/certs/env.key",
            https_cert_root="/config/certs",
        )
        monkeypatch.setattr(
            "pullbox.core.config_resolver.get_runtime_settings",
            lambda: runtime,
        )

        resp = await client.get("/settings?tab=general")

        assert resp.status_code == 200
        assert "/config/certs/env.crt" in resp.text
        assert "/config/certs/env.key" in resp.text
        assert 'data-testid="settings-general-https-enabled"' in resp.text
        assert 'data-testid="settings-general-https-cert-path"' in resp.text
        assert 'data-testid="settings-general-https-key-path"' in resp.text
        assert 'data-testid="settings-general-https-enabled"\n              disabled' in resp.text
        assert 'data-testid="settings-general-https-cert-path"\n            readonly' in resp.text
        assert 'data-testid="settings-general-https-key-path"\n            readonly' in resp.text
