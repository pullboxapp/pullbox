"""Tests for config API routing after the config simplification."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.providers.base import ProviderRegistry
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.search_runtime import build_search_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-config-routing")
os.environ.setdefault("PULLBOX_DATA_DIR", tempfile.mkdtemp())


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _session_token(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    from pullbox.models.user import User

    async with _db_factory() as session:
        user = User(
            username="configuser",
            password_hash=AuthService.hash_password("Test@1234"),
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


class TestGetConfig:
    """GET /api/v1/config returns DB-backed settings with defaults."""

    @pytest.mark.asyncio
    async def test_response_includes_identity_defaults(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200

        configs = {c["key"]: c["value"] for c in resp.json()}
        assert configs["instance_name"] == "Pullbox"
        assert configs["base_url"] == "http://localhost:8585"

    @pytest.mark.asyncio
    async def test_secret_key_not_in_response(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/config")
        keys = {c["key"] for c in resp.json()}
        assert "secret_key" not in keys


class TestPutIdentitySettings:
    """Instance name and base_url are DB-backed app settings."""

    @pytest.mark.asyncio
    async def test_put_instance_name_persists_to_db(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"instance_name": "MyComics"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200

        async with _db_factory() as session:
            row = await session.get(SystemConfig, "instance_name")
            assert row is not None
            assert row.value == "MyComics"

    @pytest.mark.asyncio
    async def test_put_base_url_normalizes_and_persists(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"base_url": "https://My-Pullbox.local:9090/"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200

        async with _db_factory() as session:
            row = await session.get(SystemConfig, "base_url")
            assert row is not None
            assert row.value == "https://My-Pullbox.local:9090"

    @pytest.mark.asyncio
    async def test_put_base_url_rejects_invalid_value(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"base_url": "ftp://example.com"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_put_app_setting_has_no_restart_required(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"search_interval_hours": "12"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200
        assert resp.json().get("restart_required") is not True


class TestPutSearchSettings:
    """Search settings saved through the UI config API feed runtime evaluation."""

    @pytest.mark.asyncio
    async def test_put_issue_size_warning_persists_and_updates_search_runtime(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"search_size_warn_issue_mb": "625"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200

        async def registry_builder(
            _session: AsyncSession,
            *,
            include_download_clients: bool = True,
        ) -> tuple[ProviderRegistry, dict[object, object]]:
            assert include_download_clients is False
            return ProviderRegistry(), {}

        async with _db_factory() as session:
            row = await session.get(SystemConfig, "search_size_warn_issue_mb")
            assert row is not None
            assert row.value == "625"

            runtime = await build_search_runtime(
                session,
                include_download_clients=False,
                registry_builder=registry_builder,
            )

        assert runtime is not None
        assert runtime.eval_kwargs["warn_issue_mb"] == 625


class TestRuntimeManagedKeys:
    """Runtime-managed keys are visible elsewhere but not editable here."""

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

    @pytest.mark.asyncio
    async def test_logs_dir_rejected(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"logs_dir": "/tmp/logs"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)
