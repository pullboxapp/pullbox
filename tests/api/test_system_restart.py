"""Tests for POST /api/v1/system/restart endpoint.

Verifies:
- Authenticated session returns 200 with restart_initiated
- Unauthenticated request returns 401
- API key auth returns 403 (InteractiveOperatorUser rejects API keys)
- CSRF token required
- ShutdownManager.schedule_restart() is called

Run:
    pytest tests/api/test_system_restart.py -v
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-restart")
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
        await conn.run_sync(Base.metadata.create_all)
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
            username="restartuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return AuthService.create_session_token(user.id, user.session_version)


@pytest.fixture
async def _api_key(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    from pullbox.models.user import User

    async with _db_factory() as session:
        user = User(
            username="apiuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        raw_key, _api_key_obj = await AuthService.generate_api_key(
            session, user_id=user.id, name="test-key"
        )
    return raw_key


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


@pytest.fixture
async def api_key_client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key: str,
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
        headers={"X-API-Key": _api_key},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Tests ────────────────────────────────────────────────────────────


class TestRestartEndpoint:
    """POST /api/v1/system/restart tests."""

    @pytest.mark.asyncio
    async def test_restart_returns_200(self, client: AsyncClient) -> None:
        with patch("pullbox.api.v1.system.shutdown_manager") as mock_mgr:
            resp = await client.post(
                "/api/v1/system/restart",
                headers=_csrf_header_for(client),
            )
        assert resp.status_code == 200
        mock_mgr.schedule_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_response_body(self, client: AsyncClient) -> None:
        with patch("pullbox.api.v1.system.shutdown_manager"):
            resp = await client.post(
                "/api/v1/system/restart",
                headers=_csrf_header_for(client),
            )
        body = resp.json()
        assert body["restart_initiated"] is True
        assert "message" in body

    @pytest.mark.asyncio
    async def test_restart_requires_auth(self, client: AsyncClient) -> None:
        """Request without valid session cookie returns 401 or 403."""
        # Clear the session cookie to simulate unauthenticated request
        client.cookies.clear()
        resp = await client.post("/api/v1/system/restart")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_restart_rejects_api_key(self, api_key_client: AsyncClient) -> None:
        """API key auth is rejected — restart requires interactive auth."""
        resp = await api_key_client.post("/api/v1/system/restart")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_restart_csrf_required(self, client: AsyncClient) -> None:
        """POST without CSRF token returns 403."""
        with patch("pullbox.api.v1.system.shutdown_manager"):
            resp = await client.post("/api/v1/system/restart")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_restart_sets_shutdown_flag(self, client: AsyncClient) -> None:
        with patch("pullbox.api.v1.system.shutdown_manager") as mock_mgr:
            mock_mgr.is_restart_pending = False
            await client.post(
                "/api/v1/system/restart",
                headers=_csrf_header_for(client),
            )
        mock_mgr.schedule_restart.assert_called_once()
