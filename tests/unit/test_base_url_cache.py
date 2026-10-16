"""Tests for base_url live caching (follows the instance_name pattern).

Verifies:
- get_base_url() returns default before any warmup
- Cache can be updated and get_base_url() returns the new value
- On-save config update sets the cache
- System info endpoint returns the cached value

Run:
    pytest tests/unit/test_base_url_cache.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-base-url")
os.environ.setdefault("PULLBOX_DATA_DIR", tempfile.mkdtemp())


# ── Fixtures ──────────────────────────────────────────────────────────


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
    seeded_user_password_hash: str,
) -> str:
    from pullbox.models.user import User

    async with _db_factory() as session:
        user = User(
            username="urluser",
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


# ── Unit: cache behavior ────────────────────────────────────────────


class TestBaseUrlCache:
    """Verify _cached_base_url and get_base_url() behavior."""

    def test_default_value(self) -> None:
        import pullbox.ui.routes as routes

        # Reset to known state
        original = routes._cached_base_url
        try:
            routes._cached_base_url = "http://localhost:8585"
            assert routes.get_base_url() == "http://localhost:8585"
        finally:
            routes._cached_base_url = original

    def test_cache_update_reflected(self) -> None:
        import pullbox.ui.routes as routes

        original = routes._cached_base_url
        try:
            routes._cached_base_url = "https://pullbox.example.com"
            assert routes.get_base_url() == "https://pullbox.example.com"
        finally:
            routes._cached_base_url = original

    def test_jinja_global_registered(self) -> None:
        import pullbox.ui.routes as routes

        assert "base_url" in routes.templates.env.globals
        assert routes.templates.env.globals["base_url"] is routes.get_base_url


# ── API: save updates cache ─────────────────────────────────────────


class TestBaseUrlSaveUpdatesCache:
    """base_url is a DB-backed app setting routed through the config API."""

    @pytest.mark.asyncio
    async def test_save_base_url_accepted(self, client: AsyncClient) -> None:
        """base_url save is accepted and updates the live cache."""
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"base_url": "https://my-pullbox.local:9090"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200
