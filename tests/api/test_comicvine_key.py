"""Tests for ComicVine API key obfuscation and management.

Verifies:
- GET config never returns the full/encrypted API key
- Secret-type config values are obfuscated in GET responses
- Saving a key stores it and returns obfuscated version
- Saving an empty key is rejected
- obfuscate_api_key handles edge cases (short keys, empty, None)

Run:
    pytest tests/api/test_comicvine_key.py -v
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.config import get_settings
from pullbox.core.comicvine_key import (
    get_comicvine_api_key,
    obfuscate_api_key,
    save_comicvine_api_key,
)
from pullbox.core.encryption import decrypt_secret, encrypt_secret
from pullbox.models.config import SystemConfig
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-comicvine")


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
            username="cvuser",
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


# ── Unit: obfuscate_api_key ──────────────────────────────────────────


class TestObfuscateApiKey:
    """Unit tests for the obfuscation helper."""

    def test_normal_key_shows_last_five(self) -> None:
        result = obfuscate_api_key("abcdef1234567890abcd")
        assert result.endswith("0abcd")
        assert result.startswith("••••")
        assert len(result) == 20

    def test_empty_key_returns_empty(self) -> None:
        assert obfuscate_api_key("") == ""

    def test_short_key_three_chars(self) -> None:
        """Key shorter than 5 chars shows all as visible (no dots)."""
        result = obfuscate_api_key("abc")
        assert result == "abc"

    def test_exactly_four_chars(self) -> None:
        result = obfuscate_api_key("abcd")
        assert result == "abcd"

    def test_five_chars(self) -> None:
        result = obfuscate_api_key("xabcd")
        assert result == "xabcd"

    def test_six_chars(self) -> None:
        result = obfuscate_api_key("yxabcd")
        assert result == "•xabcd"

    def test_one_char(self) -> None:
        result = obfuscate_api_key("x")
        assert result == "x"


# ── Unit: resolver and storage behavior ──────────────────────────────


class TestComicVineApiKeyResolver:
    """Direct resolver/storage tests for encrypted DB and env fallback behavior."""

    @pytest.mark.asyncio
    async def test_get_prefers_decrypted_database_value_over_env(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PULLBOX_COMICVINE_API_KEY", "env-key")
        get_settings.cache_clear()
        async with _db_factory() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value=encrypt_secret("db-key"),
                    value_type="secret",
                )
            )
            await session.commit()

            assert await get_comicvine_api_key(session) == "db-key"

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_get_falls_back_to_env_when_database_missing(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PULLBOX_COMICVINE_API_KEY", "env-only-key")
        get_settings.cache_clear()
        async with _db_factory() as session:
            assert await get_comicvine_api_key(session) == "env-only-key"

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_get_falls_back_to_env_when_database_secret_is_invalid(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PULLBOX_COMICVINE_API_KEY", "fallback-key")
        get_settings.cache_clear()
        async with _db_factory() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value="enc:not-a-valid-encrypted-secret",
                    value_type="secret",
                )
            )
            await session.commit()

            assert await get_comicvine_api_key(session) == "fallback-key"

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_get_returns_empty_string_when_no_key_configured(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PULLBOX_COMICVINE_API_KEY", raising=False)
        get_settings.cache_clear()
        async with _db_factory() as session:
            assert await get_comicvine_api_key(session) == ""

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_save_creates_encrypted_secret_row(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PULLBOX_COMICVINE_API_KEY", raising=False)
        get_settings.cache_clear()
        async with _db_factory() as session:
            await save_comicvine_api_key(session, "stored-key")
            await session.commit()

            config = (
                await session.execute(
                    select(SystemConfig).where(SystemConfig.key == "comicvine_api_key")
                )
            ).scalar_one()
            assert config.value_type == "secret"
            assert config.value != "stored-key"
            assert decrypt_secret(config.value) == "stored-key"
            assert await get_comicvine_api_key(session) == "stored-key"

        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_save_updates_existing_secret_row(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with _db_factory() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value=encrypt_secret("old-key"),
                    value_type="secret",
                )
            )
            await session.commit()

            await save_comicvine_api_key(session, "new-key")
            await session.commit()

            config = await session.get(SystemConfig, "comicvine_api_key")
            assert config is not None
            assert decrypt_secret(config.value) == "new-key"

    @pytest.mark.asyncio
    async def test_save_blank_key_clears_existing_secret(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PULLBOX_COMICVINE_API_KEY", raising=False)
        get_settings.cache_clear()
        async with _db_factory() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value=encrypt_secret("old-key"),
                    value_type="secret",
                )
            )
            await session.commit()

            await save_comicvine_api_key(session, "")
            await session.commit()

            config = await session.get(SystemConfig, "comicvine_api_key")
            assert config is not None
            assert config.value == ""
            assert await get_comicvine_api_key(session) == ""

        get_settings.cache_clear()


# ── API: GET config never leaks secrets ──────────────────────────────


class TestGetConfigObfuscation:
    """GET /api/v1/config must obfuscate secret-type values."""

    @pytest.mark.asyncio
    async def test_get_config_hides_secret_values(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Secret-type config values are replaced with obfuscated text."""
        async with _db_factory() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value=encrypt_secret("my_secret_key_1234"),
                    value_type="secret",
                )
            )
            await session.commit()

        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        configs = resp.json()

        cv_entry = next((c for c in configs if c["key"] == "comicvine_api_key"), None)
        assert cv_entry is not None
        # Must NOT contain the encrypted cipher text or the plaintext
        assert "my_secret_key_1234" not in cv_entry["value"]
        # Should show obfuscated dots
        assert "••••" in cv_entry["value"]

    @pytest.mark.asyncio
    async def test_get_config_no_secret_shows_not_configured(
        self,
        client: AsyncClient,
    ) -> None:
        """When no secret key is stored, value is empty string."""
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        configs = resp.json()

        cv_entry = next((c for c in configs if c["key"] == "comicvine_api_key"), None)
        # Key might not be present if not seeded, or value should be empty
        if cv_entry is not None:
            assert cv_entry["value"] == ""


# ── API: Save and verify round-trip ──────────────────────────────────


class TestSaveComicVineKey:
    """POST /api/v1/config/comicvine/save round-trip tests."""

    @pytest.mark.asyncio
    async def test_save_returns_obfuscated(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/config/comicvine/save",
            json={"api_key": "pullbox-test-comicvine-key-6c7c"},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["saved"] is True
        assert data["obfuscated"].endswith("6c7c")
        assert "•" in data["obfuscated"]

    @pytest.mark.asyncio
    async def test_save_empty_key_rejected(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/config/comicvine/save",
            json={"api_key": ""},
            headers=_csrf_header_for(client),
        )
        data = resp.json()
        assert data["saved"] is False

    @pytest.mark.asyncio
    async def test_save_whitespace_key_rejected(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/v1/config/comicvine/save",
            json={"api_key": "   "},
            headers=_csrf_header_for(client),
        )
        data = resp.json()
        assert data["saved"] is False

    @pytest.mark.asyncio
    async def test_get_config_after_save_shows_obfuscated(
        self,
        client: AsyncClient,
    ) -> None:
        """After saving a key, GET config shows obfuscated version."""
        await client.post(
            "/api/v1/config/comicvine/save",
            json={"api_key": "pullbox-test-comicvine-key-1234"},
            headers=_csrf_header_for(client),
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        configs = resp.json()

        cv_entry = next((c for c in configs if c["key"] == "comicvine_api_key"), None)
        assert cv_entry is not None
        # Should be obfuscated, not the encrypted value
        assert "••••" in cv_entry["value"]
        assert cv_entry["value"].endswith("1234")

    @pytest.mark.asyncio
    async def test_put_config_rejects_secret_key(
        self,
        client: AsyncClient,
    ) -> None:
        """Generic PUT config must reject secret keys."""
        resp = await client.put(
            "/api/v1/config",
            json={"values": {"comicvine_api_key": "should_not_work"}},
            headers=_csrf_header_for(client),
        )
        assert resp.status_code in (400, 422)


class TestComicVineKeyValidation:
    """POST /api/v1/config/comicvine/test response safety tests."""

    @pytest.mark.asyncio
    async def test_test_key_hides_unexpected_exception_details(
        self,
        client: AsyncClient,
    ) -> None:
        with patch(
            "pullbox.providers.metadata.comicvine.ComicVineProvider.test_connection",
            new=AsyncMock(
                side_effect=RuntimeError(
                    "boom reading /config/certs/server.key from https://internal.example"
                )
            ),
        ):
            resp = await client.post(
                "/api/v1/config/comicvine/test",
                json={"api_key": "pullbox-test-comicvine-key-1234"},
                headers=_csrf_header_for(client),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "healthy": False,
            "message": "Connection failed. Check Pullbox logs for details.",
        }
