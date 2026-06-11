"""Unit tests for API key security helpers and service boundaries."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import select

from pullbox.core.api_keys import (
    API_KEY_HASH_PREFIX,
    API_KEY_LENGTH,
    API_KEY_PREFIX,
    hash_api_key,
    is_well_formed_api_key,
    legacy_hash_api_key,
    normalize_api_key_name,
)
from pullbox.models.user import APIKey, User
from pullbox.schemas.auth import APIKeyCreate
from pullbox.services.auth_service import AuthService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _ExplodingSession:
    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("malformed API keys should not reach the database")


class TestAPIKeyFormat:
    def test_generated_key_shape_is_recognized(self) -> None:
        raw_key = API_KEY_PREFIX + ("a" * (API_KEY_LENGTH - len(API_KEY_PREFIX)))

        assert is_well_formed_api_key(raw_key)

    @pytest.mark.parametrize(
        "raw_key",
        [
            "",
            "not-a-pullbox-key",
            API_KEY_PREFIX + "short",
            "pb_k2_" + ("a" * 64),
            API_KEY_PREFIX + ("a" * 65),
        ],
    )
    def test_malformed_key_shape_is_rejected(self, raw_key: str) -> None:
        assert not is_well_formed_api_key(raw_key)

    @pytest.mark.asyncio
    async def test_malformed_key_does_not_touch_database(self) -> None:
        user = await AuthService.validate_api_key(
            cast("AsyncSession", _ExplodingSession()),
            API_KEY_PREFIX + "short",
        )

        assert user is None


class TestAPIKeyHashing:
    def test_hash_is_versioned_hmac_not_raw_sha256_hex(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        raw_key = API_KEY_PREFIX + ("b" * 64)
        monkeypatch.setattr("pullbox.core.api_keys.get_application_secret", lambda: "secret-one")

        hashed = hash_api_key(raw_key)

        assert hashed.startswith(API_KEY_HASH_PREFIX)
        assert hashed == hash_api_key(raw_key)
        assert hashed != hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        assert raw_key not in hashed

    def test_hash_uses_application_secret_as_pepper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw_key = API_KEY_PREFIX + ("c" * 64)
        monkeypatch.setattr("pullbox.core.api_keys.get_application_secret", lambda: "secret-one")
        first_hash = hash_api_key(raw_key)

        monkeypatch.setattr("pullbox.core.api_keys.get_application_secret", lambda: "secret-two")

        assert hash_api_key(raw_key) != first_hash


class TestAPIKeyNameNormalization:
    def test_normalizes_api_key_name_whitespace(self) -> None:
        assert normalize_api_key_name("  Nightly\nAutomation\tKey  ") == "Nightly Automation Key"

    @pytest.mark.parametrize("name", ["", "   ", "\n\t"])
    def test_rejects_blank_api_key_name(self, name: str) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            normalize_api_key_name(name)

    def test_schema_normalizes_api_key_name(self) -> None:
        body = APIKeyCreate(name="  Kitchen   Display  ")

        assert body.name == "Kitchen Display"


class TestAPIKeyServiceLifecycle:
    @pytest.mark.asyncio
    async def test_generate_api_key_stores_hash_only(self, db_session: AsyncSession) -> None:
        user = User(username="api-user", password_hash=AuthService.hash_password("Test@1234"))
        db_session.add(user)
        await db_session.flush()

        raw_key, api_key = await AuthService.generate_api_key(
            db_session,
            user.id,
            "Automation",
        )

        assert raw_key.startswith(API_KEY_PREFIX)
        assert is_well_formed_api_key(raw_key)
        assert api_key.key_hash == hash_api_key(raw_key)
        assert api_key.key_hash != raw_key
        assert raw_key not in str(api_key.__dict__)

    @pytest.mark.asyncio
    async def test_validate_api_key_updates_last_used_at(self, db_session: AsyncSession) -> None:
        user = User(
            username="active-key-user", password_hash=AuthService.hash_password("Test@1234")
        )
        db_session.add(user)
        await db_session.flush()
        raw_key, api_key = await AuthService.generate_api_key(
            db_session,
            user.id,
            "Active",
        )

        authenticated = await AuthService.validate_api_key(db_session, raw_key)

        assert authenticated is not None
        assert authenticated.id == user.id
        await db_session.refresh(api_key)
        assert api_key.last_used_at is not None

    @pytest.mark.asyncio
    async def test_validate_legacy_api_key_hash_upgrades_to_hmac(
        self,
        db_session: AsyncSession,
    ) -> None:
        user = User(
            username="legacy-key-user", password_hash=AuthService.hash_password("Test@1234")
        )
        db_session.add(user)
        await db_session.flush()
        raw_key = API_KEY_PREFIX + ("d" * 64)
        legacy_hash = legacy_hash_api_key(raw_key)
        api_key = APIKey(user_id=user.id, key_hash=legacy_hash, name="Legacy")
        db_session.add(api_key)
        await db_session.flush()

        authenticated = await AuthService.validate_api_key(db_session, raw_key)

        assert authenticated is not None
        await db_session.refresh(api_key)
        assert api_key.key_hash == hash_api_key(raw_key)
        assert api_key.key_hash != legacy_hash

    @pytest.mark.asyncio
    async def test_revoked_api_key_is_rejected(self, db_session: AsyncSession) -> None:
        user = User(
            username="revoked-key-user", password_hash=AuthService.hash_password("Test@1234")
        )
        db_session.add(user)
        await db_session.flush()
        raw_key, api_key = await AuthService.generate_api_key(
            db_session,
            user.id,
            "Revoked",
        )
        api_key.is_active = False
        await db_session.flush()

        assert await AuthService.validate_api_key(db_session, raw_key) is None

    @pytest.mark.asyncio
    async def test_expired_api_key_is_rejected_without_last_used_update(
        self,
        db_session: AsyncSession,
    ) -> None:
        user = User(
            username="expired-key-user", password_hash=AuthService.hash_password("Test@1234")
        )
        db_session.add(user)
        await db_session.flush()
        raw_key, api_key = await AuthService.generate_api_key(
            db_session,
            user.id,
            "Expired",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )

        assert await AuthService.validate_api_key(db_session, raw_key) is None
        await db_session.refresh(api_key)
        assert api_key.last_used_at is None

    @pytest.mark.asyncio
    async def test_api_key_hash_is_the_only_persisted_key_material(
        self,
        db_session: AsyncSession,
    ) -> None:
        user = User(
            username="persisted-key-user", password_hash=AuthService.hash_password("Test@1234")
        )
        db_session.add(user)
        await db_session.flush()
        raw_key, api_key = await AuthService.generate_api_key(
            db_session,
            user.id,
            "Persisted",
        )
        await db_session.flush()

        result = await db_session.execute(select(APIKey).where(APIKey.id == api_key.id))
        persisted = result.scalar_one()
        assert persisted.key_hash == hash_api_key(raw_key)
        assert raw_key not in persisted.key_hash
        assert not hasattr(persisted, "key")
