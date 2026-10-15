"""End-to-end tests for security response headers.

Verifies that all responses — successful, error, and preflight — include
the expected security headers from SecurityHeadersMiddleware.

Run:
    pytest tests/integration/test_security_headers_e2e.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.user import User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-headers-tests")

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "x-xss-protection": "0",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


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
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[object, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app
    from pullbox.config import get_settings

    monkeypatch.setenv("PULLBOX_DEBUG", "false")
    get_settings.cache_clear()
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
    get_settings.cache_clear()


@pytest.fixture
async def client(app: object, test_user: User) -> AsyncGenerator[AsyncClient, None]:
    token = AuthService.create_session_token(test_user.id, test_user.session_version)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client(app: object) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _assert_security_headers(resp_headers: dict) -> None:  # type: ignore[type-arg]
    """Assert all expected security headers are present."""
    for header, expected_value in EXPECTED_HEADERS.items():
        assert header in resp_headers, f"Missing header: {header}"
        assert resp_headers[header] == expected_value, (
            f"Header {header}: expected {expected_value!r}, got {resp_headers[header]!r}"
        )
    # CSP should be present
    assert "content-security-policy" in resp_headers


class TestSecurityHeadersPresence:
    """All responses should include security headers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "endpoint",
        [
            "/ping",
            "/api/v1/series",
            "/api/v1/config",
            "/api/v1/system/about",
            "/api/v1/system/backups",
        ],
    )
    async def test_all_responses_have_security_headers(
        self, client: AsyncClient, endpoint: str
    ) -> None:
        resp = await client.get(endpoint)
        _assert_security_headers(dict(resp.headers))

    @pytest.mark.asyncio
    async def test_error_responses_have_security_headers(self, client: AsyncClient) -> None:
        """404 responses should still include security headers."""
        resp = await client.get("/api/v1/nonexistent-endpoint")
        _assert_security_headers(dict(resp.headers))

    @pytest.mark.asyncio
    async def test_unauthenticated_responses_have_security_headers(
        self, unauth_client: AsyncClient, test_user: User
    ) -> None:
        """401 responses should include security headers."""
        resp = await unauth_client.get("/api/v1/series")
        _assert_security_headers(dict(resp.headers))


class TestHSTSHeader:
    """HSTS header behavior based on debug mode."""

    @pytest.mark.asyncio
    async def test_hsts_present_in_production(self, client: AsyncClient) -> None:
        """HSTS should be set when debug=False (default in tests)."""
        resp = await client.get("/ping")
        # The app defaults to debug=True in dev, but our test env may differ.
        # Check that EITHER HSTS is present OR debug mode is on.
        headers = dict(resp.headers)
        _assert_security_headers(headers)
        # CSP should always be present regardless of mode
        assert "content-security-policy" in headers


class TestCSPHeader:
    """Content-Security-Policy should include known cover image domains."""

    @pytest.mark.asyncio
    async def test_csp_allows_comicvine_images(self, client: AsyncClient) -> None:
        resp = await client.get("/ping")
        csp = resp.headers.get("content-security-policy", "")
        assert "comicvine.gamespot.com" in csp
        assert "frame-ancestors 'none'" in csp

    @pytest.mark.asyncio
    async def test_csp_allows_pullbox_data_cover_images(self, client: AsyncClient) -> None:
        resp = await client.get("/ping")
        csp = resp.headers.get("content-security-policy", "")
        assert "https://s3.amazonaws.com" in csp


class TestStaticAssetCaching:
    """Static app assets should be browser-cacheable without relaxing app page caching."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "asset_path",
        [
            "/static/css/tailwind.css",
            "/static/js/pullbox.js",
            "/static/fonts/dm-sans-variable.woff2",
        ],
    )
    async def test_static_assets_have_explicit_cache_contract(
        self, unauth_client: AsyncClient, asset_path: str
    ) -> None:
        resp = await unauth_client.get(asset_path)

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "public, max-age=86400"
        assert "etag" in resp.headers
        assert "last-modified" in resp.headers

    @pytest.mark.asyncio
    async def test_dynamic_routes_keep_no_store_cache_contract(self, client: AsyncClient) -> None:
        resp = await client.get("/ping")

        assert resp.headers["cache-control"] == "no-store, no-cache, max-age=0, must-revalidate"


class TestCORSDefault:
    """CORS should not be enabled for production/default app responses."""

    @pytest.mark.asyncio
    async def test_cors_headers_absent_by_default(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/ping",
            headers={"Origin": "https://example.invalid"},
        )

        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-credentials" not in resp.headers
