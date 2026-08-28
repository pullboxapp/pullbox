"""Durable shared AirDC++ hub-search cooldown contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.services.airdcpp_search_cooldown import AirDcppSearchCooldown


async def _create_client(factory: async_sessionmaker, *, interval: int = 45) -> int:
    async with factory() as session:
        client = DownloadClientConfig(
            name="Air",
            client_type=DownloadClientType.AIRDCPP,
            url="http://air.example.test",
            enabled=True,
            priority=50,
            username="pullbox",
            password="encrypted",
            remote_path="/Downloads",
            download_dir="/downloads/airdcpp",
        )
        client.airdcpp_settings = AirDcppClientSettings(minimum_search_interval_seconds=interval)
        session.add(client)
        await session.commit()
        return client.id


@pytest.mark.asyncio
async def test_cooldown_reservation_is_durable_shared_and_never_released_on_cancel(
    async_engine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    config_id = await _create_client(factory)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    first_process = AirDcppSearchCooldown(factory)

    first = await first_process.reserve(config_id, now=now)
    repeat = await first_process.reserve(config_id, now=now + timedelta(seconds=10))

    assert first.granted is True
    assert first.not_before == now
    assert first.next_allowed_at == now + timedelta(seconds=45)
    assert repeat.granted is False
    assert repeat.wait_seconds == 35
    assert repeat.next_allowed_at == first.next_allowed_at

    # A fresh service instance simulates restart; no release/cancel API exists.
    restarted = AirDcppSearchCooldown(factory)
    status = await restarted.status(config_id, now=now + timedelta(seconds=20))
    assert status.wait_seconds == 25

    next_reservation = await restarted.reserve(config_id, now=now + timedelta(seconds=45))
    assert next_reservation.granted is True
    assert next_reservation.next_allowed_at == now + timedelta(seconds=90)


@pytest.mark.asyncio
async def test_confirmed_sent_event_extends_but_never_shortens_cooldown(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    config_id = await _create_client(factory)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = AirDcppSearchCooldown(factory)
    await cooldown.reserve(config_id, now=now)

    extended = await cooldown.extend_from_sent(
        config_id,
        sent_at=now + timedelta(seconds=8),
    )
    unchanged = await cooldown.extend_from_sent(
        config_id,
        sent_at=now - timedelta(seconds=30),
    )

    assert extended == now + timedelta(seconds=53)
    assert unchanged == extended


@pytest.mark.asyncio
async def test_concurrent_reservations_allow_exactly_one_search(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    config_id = await _create_client(factory)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = AirDcppSearchCooldown(factory)

    reservations = await asyncio.gather(
        cooldown.reserve(config_id, now=now),
        cooldown.reserve(config_id, now=now),
    )

    assert sum(item.granted for item in reservations) == 1
    waiting = next(item for item in reservations if not item.granted)
    assert waiting.wait_seconds == 45
