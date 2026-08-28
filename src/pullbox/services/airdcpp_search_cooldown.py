"""Durable shared AirDC++ hub-search cooldown."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update

from pullbox.models.airdcpp import AirDcppClientSettings

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    SessionFactory = async_sessionmaker[AsyncSession] | Callable[[], AsyncSession]

_MINIMUM_SECONDS = 45
_locks: dict[tuple[int, int], asyncio.Lock] = {}


class AirDcppSearchCooldownError(Exception):
    """Raised when exact-client cooldown configuration no longer exists."""


@dataclass(frozen=True, slots=True)
class AirDcppCooldownReservation:
    """Result of an atomic per-client hub-search reservation."""

    config_id: int
    granted: bool
    not_before: datetime
    next_allowed_at: datetime
    wait_seconds: int


class AirDcppSearchCooldown:
    """Atomically reserve and extend one exact client's durable rate limit."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        config_id: int,
        *,
        now: datetime | None = None,
    ) -> AirDcppCooldownReservation:
        """Reserve before hub mutation without extending a still-active gate."""
        checked_at = _utc(now)
        lock = _locks.setdefault((id(self._session_factory), config_id), asyncio.Lock())
        async with lock, self._session_factory() as session:
            interval = await session.scalar(
                select(AirDcppClientSettings.minimum_search_interval_seconds).where(
                    AirDcppClientSettings.client_config_id == config_id
                )
            )
            if interval is None:
                raise AirDcppSearchCooldownError(
                    "AirDC++ search cooldown configuration is unavailable"
                )
            interval_seconds = max(_MINIMUM_SECONDS, int(interval))
            deadline = checked_at + timedelta(seconds=interval_seconds)
            updated = await session.execute(
                update(AirDcppClientSettings)
                .where(
                    AirDcppClientSettings.client_config_id == config_id,
                    or_(
                        AirDcppClientSettings.next_search_allowed_at.is_(None),
                        AirDcppClientSettings.next_search_allowed_at <= checked_at,
                    ),
                )
                .values(next_search_allowed_at=deadline)
                .returning(AirDcppClientSettings.client_config_id)
            )
            granted = updated.scalar_one_or_none() is not None
            if granted:
                await session.commit()
                return AirDcppCooldownReservation(
                    config_id=config_id,
                    granted=True,
                    not_before=checked_at,
                    next_allowed_at=deadline,
                    wait_seconds=0,
                )

            next_allowed_at = await session.scalar(
                select(AirDcppClientSettings.next_search_allowed_at).where(
                    AirDcppClientSettings.client_config_id == config_id
                )
            )
            await session.rollback()
            if next_allowed_at is None:
                # A concurrent delete is safe failure, never a bypass.
                raise AirDcppSearchCooldownError(
                    "AirDC++ search cooldown configuration is unavailable"
                )
            next_allowed_at = _utc(next_allowed_at)
            return AirDcppCooldownReservation(
                config_id=config_id,
                granted=False,
                not_before=next_allowed_at,
                next_allowed_at=next_allowed_at,
                wait_seconds=_remaining_seconds(next_allowed_at, checked_at),
            )

    async def status(
        self,
        config_id: int,
        *,
        now: datetime | None = None,
    ) -> AirDcppCooldownReservation:
        """Read current remaining time without reserving or extending it."""
        checked_at = _utc(now)
        async with self._session_factory() as session:
            next_allowed_at = await session.scalar(
                select(AirDcppClientSettings.next_search_allowed_at).where(
                    AirDcppClientSettings.client_config_id == config_id
                )
            )
            await session.rollback()
        if next_allowed_at is None:
            return AirDcppCooldownReservation(
                config_id=config_id,
                granted=True,
                not_before=checked_at,
                next_allowed_at=checked_at,
                wait_seconds=0,
            )
        normalized = _utc(next_allowed_at)
        remaining = _remaining_seconds(normalized, checked_at)
        return AirDcppCooldownReservation(
            config_id=config_id,
            granted=remaining == 0,
            not_before=normalized if remaining else checked_at,
            next_allowed_at=normalized,
            wait_seconds=remaining,
        )

    async def extend_from_sent(
        self,
        config_id: int,
        *,
        sent_at: datetime | None = None,
    ) -> datetime:
        """Extend to at least the configured interval after confirmed send."""
        confirmed_at = _utc(sent_at)
        lock = _locks.setdefault((id(self._session_factory), config_id), asyncio.Lock())
        async with lock, self._session_factory() as session:
            interval = await session.scalar(
                select(AirDcppClientSettings.minimum_search_interval_seconds).where(
                    AirDcppClientSettings.client_config_id == config_id
                )
            )
            if interval is None:
                raise AirDcppSearchCooldownError(
                    "AirDC++ search cooldown configuration is unavailable"
                )
            deadline = confirmed_at + timedelta(seconds=max(_MINIMUM_SECONDS, int(interval)))
            await session.execute(
                update(AirDcppClientSettings)
                .where(
                    AirDcppClientSettings.client_config_id == config_id,
                    or_(
                        AirDcppClientSettings.next_search_allowed_at.is_(None),
                        AirDcppClientSettings.next_search_allowed_at < deadline,
                    ),
                )
                .values(next_search_allowed_at=deadline)
            )
            current = await session.scalar(
                select(AirDcppClientSettings.next_search_allowed_at).where(
                    AirDcppClientSettings.client_config_id == config_id
                )
            )
            await session.commit()
            if current is None:
                raise AirDcppSearchCooldownError(
                    "AirDC++ search cooldown configuration is unavailable"
                )
            return _utc(current)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _remaining_seconds(deadline: datetime, now: datetime) -> int:
    return max(0, math.ceil((deadline - now).total_seconds()))
