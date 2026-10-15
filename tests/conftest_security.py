"""Shared security test fixtures for integration tests.

Provides a test FastAPI app with all middleware, an in-memory database,
a seeded test user, and helper functions for creating session tokens.

Run:
    pytest tests/integration/test_security_endpoints.py -v
    pytest tests/integration/test_security_headers_e2e.py -v
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Ensure a secret key is set for tests
os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-security-tests")


@pytest.fixture
async def sec_db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await create_test_schema(conn)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def sec_user(
    sec_db: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> User:
    """Create a test user with known credentials."""
    async with sec_db() as session:
        user = User(
            username="testuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def sec_api_key(
    sec_db: async_sessionmaker[AsyncSession],
    sec_user: User,
) -> str:
    """Create a valid API key and return the raw key string."""
    raw_key = "pb_k1_" + "a" * 64
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    async with sec_db() as session:
        api_key = APIKey(
            user_id=sec_user.id,
            key_hash=key_hash,
            name="test-key",
        )
        session.add(api_key)
        await session.commit()
    return raw_key


def create_session_token_for_user(user: User) -> str:
    """Create a valid signed session token for the given user."""
    return AuthService.create_session_token(user.id, user.session_version)


@pytest.fixture
async def sec_app(
    sec_db: async_sessionmaker[AsyncSession],
) -> object:
    """Create a test FastAPI app with all middleware and overridden DB."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    app.state.db_session_factory = sec_db

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with sec_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db

    # Reset setup cache so tests start fresh
    reset_setup_cache()

    yield app

    # Clean up
    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def authenticated_client(
    sec_app: object,
    sec_user: User,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with a valid session cookie."""
    token = create_session_token_for_user(sec_user)
    transport = ASGITransport(app=sec_app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


@pytest.fixture
async def unauthenticated_client(
    sec_app: object,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with no authentication."""
    transport = ASGITransport(app=sec_app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as ac:
        yield ac
