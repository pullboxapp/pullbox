"""Closed artifact-host settings service tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.encryption import _get_fernet, is_encrypted
from pullbox.core.exceptions import ValidationError
from pullbox.models.direct_acquisition import (
    DirectArtifactHostKind,
    DirectHostAccountState,
    DirectHostConfig,
    DirectHostOperationalResult,
    DirectHostReachabilityState,
)
from pullbox.services.direct_host_settings import (
    list_direct_host_settings,
    update_direct_host_setting,
)
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator


@pytest.fixture(autouse=True)
def _deterministic_application_secret() -> Iterator[None]:
    provider = MagicMock()
    provider.secret_key.return_value = "direct-host-settings-test-secret"
    _get_fernet.cache_clear()
    with patch("pullbox.core.config_file.get_config_provider", return_value=provider):
        yield
    _get_fernet.cache_clear()


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await create_test_schema(connection)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_exposes_every_closed_host_without_writing_defaults(
    session: AsyncSession,
) -> None:
    settings = await list_direct_host_settings(session)

    assert [setting.host_kind for setting in settings] == sorted(
        DirectArtifactHostKind,
        key=lambda host_kind: host_kind.value,
    )
    assert all(setting.id is None for setting in settings)
    assert all(setting.enabled is False for setting in settings)
    pixeldrain = next(
        setting for setting in settings if setting.host_kind is DirectArtifactHostKind.PIXELDRAIN
    )
    assert pixeldrain.allowed_credential_fields == ("api_key",)
    mediafire = next(
        setting for setting in settings if setting.host_kind is DirectArtifactHostKind.MEDIAFIRE
    )
    assert mediafire.allowed_credential_fields == ()
    mega = next(setting for setting in settings if setting.host_kind is DirectArtifactHostKind.MEGA)
    assert mega.allowed_credential_fields == ("session",)
    datanodes = next(
        setting for setting in settings if setting.host_kind is DirectArtifactHostKind.DATANODES
    )
    assert datanodes.allowed_credential_fields == ("username", "password")


@pytest.mark.asyncio
async def test_list_orders_hosts_by_ascending_preference_then_name(
    session: AsyncSession,
) -> None:
    for host_kind, preference in (
        (DirectArtifactHostKind.GENERIC_HTTPS, 30),
        (DirectArtifactHostKind.ROOTZ, 10),
        (DirectArtifactHostKind.MEDIAFIRE, 10),
        (DirectArtifactHostKind.PIXELDRAIN, 20),
    ):
        await update_direct_host_setting(
            session,
            host_kind,
            enabled=False,
            preference=preference,
            credential_updates=None,
        )

    settings = await list_direct_host_settings(session)

    assert [setting.host_kind for setting in settings] == [
        DirectArtifactHostKind.MEDIAFIRE,
        DirectArtifactHostKind.ROOTZ,
        DirectArtifactHostKind.PIXELDRAIN,
        DirectArtifactHostKind.GENERIC_HTTPS,
        DirectArtifactHostKind.DATANODES,
        DirectArtifactHostKind.MEGA,
        DirectArtifactHostKind.TERABOX,
    ]


@pytest.mark.asyncio
async def test_update_creates_encrypted_write_only_host_setting(
    session: AsyncSession,
) -> None:
    setting = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
        preference=10,
        credential_updates={"api_key": "private-pixeldrain-key"},
    )

    assert setting.id is not None
    assert setting.enabled is True
    assert setting.preference == 10
    assert setting.account_state is DirectHostAccountState.UNKNOWN
    assert setting.credentials_configured is True
    assert setting.configured_credential_fields == ("api_key",)
    assert "private-pixeldrain-key" not in repr(setting)
    stored = await session.get(DirectHostConfig, setting.id)
    assert stored is not None
    assert is_encrypted(str(stored.encrypted_credentials["api_key"]))


@pytest.mark.asyncio
async def test_update_preserves_secret_when_only_preference_changes(
    session: AsyncSession,
) -> None:
    created = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.MEGA,
        enabled=True,
        preference=50,
        credential_updates={"session": "revocable-session"},
    )

    updated = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.MEGA,
        enabled=None,
        preference=5,
        credential_updates=None,
    )

    assert updated.id == created.id
    assert updated.enabled is True
    assert updated.preference == 5
    assert updated.configured_credential_fields == ("session",)
    stored = await session.get(DirectHostConfig, updated.id)
    assert stored is not None
    assert is_encrypted(str(stored.encrypted_credentials["session"]))


@pytest.mark.asyncio
async def test_empty_credential_update_preserves_host_status_history(
    session: AsyncSession,
) -> None:
    checked_at = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    created = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
        preference=50,
        credential_updates={"api_key": "private-pixeldrain-key"},
    )
    stored = await session.get(DirectHostConfig, created.id)
    assert stored is not None
    stored.account_state = DirectHostAccountState.HEALTHY
    stored.reachability_state = DirectHostReachabilityState.REACHABLE
    stored.last_tested_at = checked_at
    stored.last_reachable_at = checked_at
    stored.last_operational_result = DirectHostOperationalResult.SUCCESSFUL
    stored.last_operational_at = checked_at
    await session.commit()

    updated = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
        preference=10,
        credential_updates={},
    )

    assert updated.preference == 10
    assert updated.account_state is DirectHostAccountState.HEALTHY
    assert updated.reachability_state is DirectHostReachabilityState.REACHABLE
    assert updated.last_checked_at == checked_at
    assert updated.last_reachable_at == checked_at
    assert updated.last_operational_result is DirectHostOperationalResult.SUCCESSFUL
    assert updated.last_operational_at == checked_at


@pytest.mark.asyncio
async def test_mega_rejects_obsolete_application_key_setting(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValidationError, match="Unsupported credential field"):
        await update_direct_host_setting(
            session,
            DirectArtifactHostKind.MEGA,
            enabled=True,
            preference=50,
            credential_updates={"app_key": "obsolete-application-key"},
        )


@pytest.mark.asyncio
async def test_required_account_host_cannot_be_enabled_without_session(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValidationError, match="requires an account session"):
        await update_direct_host_setting(
            session,
            DirectArtifactHostKind.TERABOX,
            enabled=True,
            preference=50,
            credential_updates=None,
        )

    settings = await list_direct_host_settings(session)
    terabox = next(
        setting for setting in settings if setting.host_kind is DirectArtifactHostKind.TERABOX
    )
    assert terabox.id is None
    assert terabox.enabled is False


@pytest.mark.asyncio
async def test_datanodes_requires_a_complete_account_before_enabling(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValidationError, match="both username and password"):
        await update_direct_host_setting(
            session,
            DirectArtifactHostKind.DATANODES,
            enabled=True,
            preference=50,
            credential_updates={"username": "reader@example.test"},
        )

    settings = await list_direct_host_settings(session)
    datanodes = next(
        setting for setting in settings if setting.host_kind is DirectArtifactHostKind.DATANODES
    )
    assert datanodes.id is None
    assert datanodes.enabled is False


@pytest.mark.asyncio
async def test_datanodes_stores_a_complete_write_only_account(
    session: AsyncSession,
) -> None:
    setting = await update_direct_host_setting(
        session,
        DirectArtifactHostKind.DATANODES,
        enabled=True,
        preference=30,
        credential_updates={
            "username": "reader@example.test",
            "password": "private-password",
        },
    )

    assert setting.enabled is True
    assert setting.configured_credential_fields == ("password", "username")
    assert "reader@example.test" not in repr(setting)
    assert "private-password" not in repr(setting)
    stored = await session.get(DirectHostConfig, setting.id)
    assert stored is not None
    assert is_encrypted(str(stored.encrypted_credentials["username"]))
    assert is_encrypted(str(stored.encrypted_credentials["password"]))
