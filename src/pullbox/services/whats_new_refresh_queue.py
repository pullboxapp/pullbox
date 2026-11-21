"""Refresh queue coordinator for What's New cache refresh requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.models.whats_new import WhatsNewReleaseCache
    from pullbox.services.whats_new_cache_service import WhatsNewCacheService
    from pullbox.services.whats_new_refresh_service import WhatsNewReleaseClient

logger = structlog.get_logger(__name__)


class BackgroundTaskSink(Protocol):
    """Subset of FastAPI BackgroundTasks used by the refresh coordinator."""

    def add_task(self, func: Callable[[], Awaitable[None]]) -> None: ...


class RefreshQueueStatus(StrEnum):
    """Possible outcomes when requesting a refresh."""

    QUEUED = "queued"
    ALREADY_RUNNING = "already_running"


class StartupRefreshStatus(StrEnum):
    """Possible outcomes for startup freshness checks."""

    REFRESHED = "refreshed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class RefreshQueueResult:
    """Result returned by the refresh queue coordinator."""

    status: RefreshQueueStatus
    message: str


@dataclass(frozen=True)
class StartupRefreshResult:
    """Result returned by startup cache freshness checks."""

    status: StartupRefreshStatus
    reason: str


async def noop_refresh_runner() -> None:
    """Placeholder refresh runner until PD-6.4 wires upstream refresh behavior."""


async def run_whats_new_refresh(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: WhatsNewReleaseClient | None = None,
) -> None:
    """Fetch pullbox-data releases and commit them to the local cache."""

    from pullbox.database import get_session_factory
    from pullbox.services.whats_new_refresh_service import WhatsNewRefreshService
    from pullbox.services.whats_new_watch_add import finish_watch_adds

    factory = session_factory or get_session_factory()
    async with factory() as session:
        try:
            service = WhatsNewRefreshService(client=client)
            result = await service.refresh(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    await finish_watch_adds(factory, result.watch_adds)


async def refresh_whats_new_cache_if_needed(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: WhatsNewReleaseClient | None = None,
) -> StartupRefreshResult:
    """Refresh the cache at startup when it is missing or stale."""

    from datetime import UTC, datetime

    from pullbox.config import get_settings
    from pullbox.database import get_session_factory
    from pullbox.services.whats_new_actions import published_watch_evidence
    from pullbox.services.whats_new_cache_service import WhatsNewCacheService
    from pullbox.services.whats_new_refresh_service import WhatsNewRefreshService
    from pullbox.services.whats_new_watch_add import finish_watch_adds, pending_adds

    factory = session_factory or get_session_factory()
    async with factory() as session:
        cache = WhatsNewCacheService()
        current = await cache.get_latest_current_week(session)
        upcoming = await cache.get_upcoming(session)
        reason = _startup_refresh_reason(cache, current, upcoming)
        if (
            reason is None
            and current
            and upcoming
            and get_settings().metadata_whats_new_actions_enabled
        ):
            observed = datetime.now(UTC).date()
            evidence = published_watch_evidence(current.payload, upcoming.payload, as_of=observed)
            if await pending_adds(
                session, evidence, observed, upcoming.fetched_at, interrupted_only=True
            ):
                # Cached evidence can request a new observation, never authorize a stale Add.
                reason = "pending_watch"
        if reason is None:
            return StartupRefreshResult(status=StartupRefreshStatus.SKIPPED, reason="fresh")

        try:
            await session.rollback()  # Do not hold the startup cache read across network I/O.
            service = WhatsNewRefreshService(client=client, cache=cache)
            result = await service.refresh(session)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("whats_new_startup_refresh_failed", reason=reason, exc_info=True)
            return StartupRefreshResult(status=StartupRefreshStatus.FAILED, reason=reason)

    await finish_watch_adds(factory, result.watch_adds)
    logger.info("whats_new_startup_refresh_complete", reason=reason)
    return StartupRefreshResult(status=StartupRefreshStatus.REFRESHED, reason=reason)


def _startup_refresh_reason(
    cache: WhatsNewCacheService,
    current: WhatsNewReleaseCache | None,
    upcoming: WhatsNewReleaseCache | None,
) -> str | None:
    if current is None or upcoming is None:
        return "missing"
    if cache.is_stale(current) or cache.is_stale(upcoming):
        return "stale"
    return None


class WhatsNewRefreshCoordinator:
    """Coordinates manual refresh requests and prevents overlapping refreshes."""

    def __init__(
        self,
        *,
        runner: Callable[[], Awaitable[None]] = noop_refresh_runner,
    ) -> None:
        self._runner = runner
        self._lock = asyncio.Lock()
        self._running = False

    async def queue_refresh(self, background_tasks: BackgroundTaskSink) -> RefreshQueueResult:
        """Queue a refresh unless one is already running."""

        async with self._lock:
            if self._running:
                return RefreshQueueResult(
                    status=RefreshQueueStatus.ALREADY_RUNNING,
                    message="What's New refresh is already in progress.",
                )
            self._running = True
            background_tasks.add_task(self._run)
            return RefreshQueueResult(
                status=RefreshQueueStatus.QUEUED,
                message="What's New refresh queued.",
            )

    async def _run(self) -> None:
        try:
            await self._runner()
        finally:
            async with self._lock:
                self._running = False
