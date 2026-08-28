"""AirDC++ settings persistence and validation contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pullbox.config import PullboxSettings
from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.schemas.airdcpp import AirDcppSettingsInput

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _airdcpp_client(session: AsyncSession) -> DownloadClientConfig:
    client = DownloadClientConfig(
        name="AirDC++",
        client_type=DownloadClientType.AIRDCPP,
        url="http://airdcpp.test:5600",
        username="pullbox",
        password="encrypted",
        download_dir="/downloads/airdcpp",
        remote_path="/Downloads",
    )
    session.add(client)
    await session.flush()
    return client


async def test_settings_defaults_and_one_to_one_relationship(
    db_session: AsyncSession,
) -> None:
    client = await _airdcpp_client(db_session)
    settings = AirDcppClientSettings(client_config=client)
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(settings)

    assert client.airdcpp_settings is settings
    assert settings.search_enabled is True
    assert settings.automatic_search_enabled is False
    assert settings.minimum_search_interval_seconds == 45
    assert settings.manual_collection_seconds == 8
    assert settings.automatic_collection_seconds == 15
    assert settings.max_results == 200
    assert settings.max_retained_routes == 400
    assert settings.max_concurrent_searches == 1
    assert settings.request_timeout_seconds == 15
    assert settings.search_dispatch_deadline_seconds == 45
    assert settings.reconciliation_interval_seconds == 30
    assert settings.hub_allowlist == []
    assert settings.queue_priority is None
    assert settings.next_search_allowed_at is None


async def test_settings_are_deleted_with_the_client(db_session: AsyncSession) -> None:
    client = await _airdcpp_client(db_session)
    settings = AirDcppClientSettings(client_config=client)
    db_session.add(settings)
    await db_session.flush()
    settings_id = settings.id

    await db_session.delete(client)
    await db_session.flush()

    assert await db_session.get(AirDcppClientSettings, settings_id) is None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"minimum_search_interval_seconds": 44}, "minimum_search_interval"),
        ({"manual_collection_seconds": 0}, "manual_collection"),
        ({"automatic_collection_seconds": 121}, "automatic_collection"),
        ({"max_results": 0}, "max_results"),
        ({"max_results": 500, "max_retained_routes": 499}, "retained_routes"),
        ({"max_concurrent_searches": 5}, "concurrent_searches"),
        ({"request_timeout_seconds": 121}, "request_timeout"),
        ({"search_dispatch_deadline_seconds": 4}, "dispatch_deadline"),
        ({"reconciliation_interval_seconds": 301}, "reconciliation_interval"),
        ({"queue_priority": 7}, "queue_priority"),
    ],
)
async def test_database_constraints_reject_out_of_bounds_settings(
    db_session: AsyncSession,
    overrides: dict[str, int],
    constraint: str,
) -> None:
    client = await _airdcpp_client(db_session)
    settings = AirDcppClientSettings(client_config_id=client.id, **overrides)
    db_session.add(settings)

    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()


async def test_only_one_settings_row_is_allowed_per_client(db_session: AsyncSession) -> None:
    client = await _airdcpp_client(db_session)
    db_session.add_all(
        [
            AirDcppClientSettings(client_config_id=client.id),
            AirDcppClientSettings(client_config_id=client.id),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


def test_settings_input_normalizes_and_deduplicates_hub_allowlist() -> None:
    settings = AirDcppSettingsInput(
        hub_allowlist=[
            " ADCS://Example.TEST:1511/ ",
            "adcs://example.test:1511",
            "dchub://Legacy.Example.TEST:411/",
        ]
    )

    assert settings.hub_allowlist == [
        "adcs://example.test:1511",
        "dchub://legacy.example.test:411",
    ]


@pytest.mark.parametrize(
    "hub_url",
    [
        "https://example.test",
        "adcs://user:secret@example.test:1511",
        "adcs://example.test:1511/?kp=secret",
        "adcs://example.test:1511/#fragment",
        "adcs://",
    ],
)
def test_settings_input_rejects_unsafe_or_invalid_hub_urls(hub_url: str) -> None:
    with pytest.raises(ValidationError):
        AirDcppSettingsInput(hub_allowlist=[hub_url])


def test_settings_input_rejects_more_than_100_hubs() -> None:
    with pytest.raises(ValidationError):
        AirDcppSettingsInput(
            hub_allowlist=[f"adcs://hub-{index}.example.test:1511" for index in range(101)]
        )


def test_airdcpp_feature_flag_defaults_off_and_uses_environment(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PULLBOX_AIRDCPP_ENABLED", raising=False)
    assert PullboxSettings().airdcpp_enabled is False

    monkeypatch.setenv("PULLBOX_AIRDCPP_ENABLED", "true")
    assert PullboxSettings().airdcpp_enabled is True


async def test_settings_row_can_be_loaded_by_exact_client_id(db_session: AsyncSession) -> None:
    client = await _airdcpp_client(db_session)
    db_session.add(AirDcppClientSettings(client_config_id=client.id))
    await db_session.flush()

    result = await db_session.execute(
        select(AirDcppClientSettings).where(AirDcppClientSettings.client_config_id == client.id)
    )

    assert result.scalar_one().client_config_id == client.id
