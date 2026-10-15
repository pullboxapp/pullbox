"""End-to-end integration tests for security controls.

Tests that authentication, API key validation, CSRF enforcement, and
setup blocking work correctly through the full middleware stack.

Run:
    pytest tests/integration/test_security_endpoints.py -v
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Ensure a secret key is set for tests
os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-security-ep-tests")

# Protected endpoints that require authentication
PROTECTED_ENDPOINTS = [
    "/api/v1/series",
    "/api/v1/library/unmatched",
    "/api/v1/library/stats",
    "/api/v1/downloads/queue",
    "/api/v1/config",
    "/api/v1/system/about",
    "/api/v1/system/backups",
    "/api/v1/filesystem/directories",
    "/api/v1/indexers",
]


def _csrf_from_client(client: AsyncClient) -> str:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    return AuthService.get_csrf_token_from_session(token) or ""


@pytest.fixture
async def _test_db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def test_user(
    _test_db: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> User:
    async with _test_db() as session:
        user = User(
            username="testuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def app(
    _test_db: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[object, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    application = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _test_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()
    yield application
    application.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def unauth_client(app: object) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
async def auth_client(app: object, test_user: User) -> AsyncGenerator[AsyncClient, None]:
    token = AuthService.create_session_token(test_user.id, test_user.session_version)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


class TestUnauthenticatedAccess:
    """Unauthenticated requests to protected endpoints return 401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    async def test_unauthenticated_api_returns_401(
        self, unauth_client: AsyncClient, test_user: User, endpoint: str
    ) -> None:
        resp = await unauth_client.get(endpoint)
        # Accept 401 (unauthenticated) or 302 (redirect to login for HTML)
        assert resp.status_code in (401, 302, 403), f"{endpoint} returned {resp.status_code}"


class TestAuthenticatedAccess:
    """Authenticated requests to protected endpoints succeed."""

    @pytest.mark.asyncio
    async def test_authenticated_series_succeeds(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/api/v1/series")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_authenticated_config_succeeds(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/api/v1/config")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_authenticated_about_succeeds(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/api/v1/system/about")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_authenticated_backups_succeeds(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/api/v1/system/backups")
        assert resp.status_code == 200


class TestAPIKeyAuthentication:
    """API key authentication via X-API-Key header."""

    @pytest.mark.asyncio
    async def test_valid_api_key_authenticates(
        self,
        unauth_client: AsyncClient,
        _test_db: async_sessionmaker[AsyncSession],
        test_user: User,
    ) -> None:
        raw_key = "pb_k1_" + "b" * 64
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        async with _test_db() as session:
            api_key = APIKey(
                user_id=test_user.id,
                key_hash=key_hash,
                name="test-key",
            )
            session.add(api_key)
            await session.commit()

        resp = await unauth_client.get(
            "/api/v1/series",
            headers={"X-API-Key": raw_key},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_expired_api_key_rejected(
        self,
        _test_db: async_sessionmaker[AsyncSession],
        test_user: User,
    ) -> None:
        """Expired API keys should not authenticate."""
        raw_key = "pb_k1_" + "c" * 64
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        async with _test_db() as session:
            api_key = APIKey(
                user_id=test_user.id,
                key_hash=key_hash,
                name="expired-key",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
            session.add(api_key)
            await session.commit()

        async with _test_db() as session:
            user = await AuthService.validate_api_key(session, raw_key)
            assert user is None

    @pytest.mark.asyncio
    async def test_invalid_api_key_rejected(
        self, unauth_client: AsyncClient, test_user: User
    ) -> None:
        resp = await unauth_client.get(
            "/api/v1/series",
            headers={"X-API-" + "Key": "pb_k1_" + "invalid_key"},
        )
        assert resp.status_code in (401, 302, 403)


class TestAPIKeyManagementSecurity:
    """Interactive API key management does not leak stored secrets."""

    @pytest.mark.asyncio
    async def test_create_normalizes_name_and_never_returns_hash(
        self,
        auth_client: AsyncClient,
        _test_db: async_sessionmaker[AsyncSession],
        test_user: User,
    ) -> None:
        csrf = _csrf_from_client(auth_client)

        resp = await auth_client.post(
            "/api/v1/auth/apikeys",
            json={"name": "  Nightly\nAutomation\tKey  "},
            headers={"X-CSRF-Token": csrf},
        )

        assert resp.status_code == 201
        body = resp.json()
        raw_key = body["key"]
        assert raw_key.startswith("pb_k1_")
        assert body["api_key"]["name"] == "Nightly Automation Key"
        assert "key_hash" not in body["api_key"]
        assert raw_key not in str(body["api_key"])

        async with _test_db() as session:
            result = await session.execute(select(APIKey).where(APIKey.user_id == test_user.id))
            api_key = result.scalar_one()
            assert api_key.name == "Nightly Automation Key"
            assert api_key.key_hash != raw_key

    @pytest.mark.asyncio
    async def test_list_never_returns_raw_key_or_hash(
        self,
        auth_client: AsyncClient,
    ) -> None:
        csrf = _csrf_from_client(auth_client)
        create_resp = await auth_client.post(
            "/api/v1/auth/apikeys",
            json={"name": "List Safety"},
            headers={"X-CSRF-Token": csrf},
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]

        resp = await auth_client.get("/api/v1/auth/apikeys")

        assert resp.status_code == 200
        keys = resp.json()
        assert keys
        assert all("key_hash" not in item for item in keys)
        assert raw_key not in str(keys)


class TestDeactivatedUser:
    """Deactivated users cannot authenticate."""

    @pytest.mark.asyncio
    async def test_deactivated_user_rejected(
        self,
        app: object,
        _test_db: async_sessionmaker[AsyncSession],
    ) -> None:
        # Create a deactivated user
        async with _test_db() as session:
            user = User(
                username="inactive",
                password_hash=AuthService.hash_password("Test@1234"),
                is_active=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        token = AuthService.create_session_token(user_id, 0)
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={SESSION_COOKIE_NAME: token},
        ) as client:
            resp = await client.get("/api/v1/series")
            assert resp.status_code in (401, 302, 403)


class TestSetupBlocking:
    """Setup endpoint is blocked after first user creation."""

    @pytest.mark.asyncio
    async def test_setup_blocked_after_first_user(
        self,
        unauth_client: AsyncClient,
        test_user: User,
    ) -> None:
        resp = await unauth_client.post(
            "/api/v1/system/setup",
            json={"username": "second_admin", "password": "Second@1234"},
        )
        assert resp.status_code == 401


class TestSessionVersioning:
    """Session tokens with outdated version are rejected."""

    @pytest.mark.asyncio
    async def test_outdated_session_version_rejected(
        self,
        app: object,
        _test_db: async_sessionmaker[AsyncSession],
        test_user: User,
    ) -> None:
        # Create token with version 0
        token = AuthService.create_session_token(test_user.id, 0)

        # Bump the user's session version to 1
        async with _test_db() as session:
            from sqlalchemy import select

            result = await session.execute(select(User).where(User.id == test_user.id))
            user = result.scalar_one()
            user.session_version = 1
            await session.commit()

        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={SESSION_COOKIE_NAME: token},
        ) as client:
            resp = await client.get("/api/v1/series")
            assert resp.status_code in (401, 302, 403)
