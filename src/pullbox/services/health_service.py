"""Health check engine — runs component checks and persists results.

Provides 7 component health checks (database, filesystem, ComicVine,
download clients, indexers, scheduler, system resources), each isolated
with a 10-second timeout.  Results include actionable guidance so users
know *how* to fix problems, not just that something is wrong.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from typing import TYPE_CHECKING, Any

import psutil
import structlog

from pullbox.core.sqlite_lock import SQLITE_LOCK_RETRY_ATTEMPTS, sqlite_lock_retry_delay
from pullbox.database import get_session_factory
from pullbox.models.health import HealthStatus
from pullbox.services.health_comicvine_checks import check_comicvine
from pullbox.services.health_database_checks import (
    check_database,
    check_database_query_latency,
    check_database_round_trip,
    check_db_bloat,
    check_db_integrity,
    check_db_size,
)
from pullbox.services.health_download_client_checks import (
    check_download_client_subject,
    check_download_clients,
    download_client_bootstrap_outcome,
    download_client_unknown_outcome,
)
from pullbox.services.health_filesystem_checks import (
    check_filesystem,
    check_filesystem_target,
)
from pullbox.services.health_indexer_checks import (
    check_indexer_subject,
    check_indexers,
    check_jackett_subject,
    check_prowlarr_subject,
    load_jackett_subject_config,
    load_prowlarr_subject_config,
)
from pullbox.services.health_persistence import (
    cleanup_health_history,
    get_health_history,
    get_health_incidents,
    get_overall_health_status,
    persist_health_outcomes,
    should_skip_comicvine_check,
    stage_health_outcome_rows,
)
from pullbox.services.health_scheduler_checks import check_scheduler
from pullbox.services.health_system_checks import (
    check_system_cpu,
    check_system_disk,
    check_system_memory,
    check_system_resources,
    check_system_swap,
)
from pullbox.services.health_types import CheckOutcome, SubCheckOutcome

__all__ = ["CheckOutcome", "HealthService", "SubCheckOutcome"]

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.config import PullboxSettings
    from pullbox.core.scheduler import PullboxScheduler
    from pullbox.models.client import DownloadClientConfig
    from pullbox.models.health import HealthCheckResult as HealthCheckResultModel
    from pullbox.models.health import HealthIncident as HealthIncidentModel
    from pullbox.models.indexer import IndexerConfig
    from pullbox.providers.base import DownloadClient, ProviderRegistry
    from pullbox.services.direct_resolver_service import NativeResolverOption

logger = structlog.get_logger(__name__)

_CHECK_TIMEOUT_SECONDS = 10
_COMICVINE_CHECK_INTERVAL_MINUTES = 30

# ---------------------------------------------------------------------------
# Health service
# ---------------------------------------------------------------------------


class HealthService:
    """Runs health checks against all system components.

    Args:
        settings: Application configuration.
        registry: Provider registry with download clients, indexers, and
            metadata providers.  ``None`` when no providers are configured.
        scheduler: The application task scheduler.
    """

    def __init__(
        self,
        settings: PullboxSettings | None = None,
        registry: ProviderRegistry | None = None,
        scheduler: PullboxScheduler | None = None,
        bootstrap_errors: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._scheduler = scheduler
        self._bootstrap_errors = bootstrap_errors or {}

    # ── Public API ────────────────────────────────────────────────

    async def run_all_checks(
        self,
        session: AsyncSession,
    ) -> list[CheckOutcome]:
        """Execute every health check, persist results, and return outcomes.

        ComicVine is throttled to once per ``_COMICVINE_CHECK_INTERVAL_MINUTES``
        to avoid unnecessary API calls.  Other checks run every cycle.
        """
        outcomes: list[CheckOutcome] = []

        # ComicVine: skip if last result is recent enough
        skip_comicvine = await self._should_skip_comicvine(session)

        # Use lambdas for lazy coroutine creation so skipped checks
        # don't produce unawaited-coroutine warnings.
        checks: list[tuple[Any, str, str]] = [
            (lambda: self._check_database(session), "database", "connectivity"),
            (lambda: self._check_filesystem(session), "filesystem", "accessibility"),
            (lambda: self._check_comicvine(), "comicvine", "api_connectivity"),
            (lambda: self._check_download_clients(session), "download_clients", "connectivity"),
            (lambda: self._check_indexers(session), "indexers", "connectivity"),
            (lambda: self._check_scheduler(), "scheduler", "status"),
            (lambda: self._check_system_resources(), "system", "resources"),
        ]

        for factory, component, check_name in checks:
            if component == "comicvine" and skip_comicvine:
                continue
            results = await self._safe_run(
                factory(),
                component,
                check_name,
                session=session,
            )
            outcomes.extend(results)

        await self._persist_outcomes(session, outcomes)
        return outcomes

    async def run_check(
        self,
        session: AsyncSession,
        component: str,
    ) -> list[CheckOutcome]:
        """Run checks for a single component and persist results."""
        # Use lambdas for lazy coroutine creation to avoid unawaited warnings
        dispatch: dict[str, tuple[Any, str]] = {
            "database": (lambda: self._check_database(session), "connectivity"),
            "filesystem": (lambda: self._check_filesystem(session), "accessibility"),
            "comicvine": (lambda: self._check_comicvine(), "api_connectivity"),
            "download_clients": (lambda: self._check_download_clients(session), "connectivity"),
            "indexers": (lambda: self._check_indexers(session), "connectivity"),
            "scheduler": (lambda: self._check_scheduler(), "status"),
            "system": (lambda: self._check_system_resources(), "resources"),
        }

        if component not in dispatch:
            return [
                CheckOutcome(
                    component=component,
                    check_name="unknown",
                    status=HealthStatus.UNKNOWN,
                    message=f"Unknown component: {component}",
                    actionable_guidance="Check the component name and try again.",
                )
            ]

        factory, check_name = dispatch[component]
        outcomes = await self._safe_run(
            factory(),
            component,
            check_name,
            session=session,
        )
        await self._persist_outcomes(session, outcomes)
        return outcomes

    @staticmethod
    async def get_overall_status(session: AsyncSession) -> HealthStatus:
        """Return the worst status across current top-level component summaries."""
        return await get_overall_health_status(session)

    @staticmethod
    async def get_history(
        session: AsyncSession,
        component: str | None = None,
        limit: int = 50,
        *,
        is_summary: bool | None = None,
        subject_key: str | None = None,
    ) -> list[HealthCheckResultModel]:
        """Retrieve recent health check results, optionally filtered by component."""
        return await get_health_history(
            session,
            component=component,
            limit=limit,
            is_summary=is_summary,
            subject_key=subject_key,
        )

    @staticmethod
    async def cleanup_history(
        session: AsyncSession,
        retention_days: int,
    ) -> int:
        """Delete health check results older than *retention_days*."""
        return await cleanup_health_history(session, retention_days)

    @staticmethod
    async def get_incidents(
        session: AsyncSession,
        component: str | None = None,
        limit: int = 50,
        *,
        include_resolved: bool = True,
    ) -> list[HealthIncidentModel]:
        """Retrieve compact long-term health incidents."""
        return await get_health_incidents(
            session,
            component=component,
            limit=limit,
            include_resolved=include_resolved,
        )

    # ── Individual checks ─────────────────────────────────────────

    @staticmethod
    async def _should_skip_comicvine(session: AsyncSession) -> bool:
        """Return True if the last ComicVine check is recent enough to reuse."""
        return await should_skip_comicvine_check(
            session,
            interval_minutes=_COMICVINE_CHECK_INTERVAL_MINUTES,
        )

    async def _check_database(
        self,
        session: AsyncSession,
    ) -> CheckOutcome:
        """Verify database connectivity, representative read performance, and SQLite health."""
        return await check_database(
            session,
            check_round_trip=self._check_database_round_trip,
            check_query_latency=self._check_database_query_latency,
            check_size=self._check_db_size,
            check_integrity=self._check_db_integrity,
            check_bloat=self._check_db_bloat,
        )

    async def _check_database_round_trip(self, session: AsyncSession) -> SubCheckOutcome:
        """Measure a lightweight connection round-trip for the current database."""
        return await check_database_round_trip(session, perf_counter=time.perf_counter)

    async def _check_database_query_latency(self, session: AsyncSession) -> SubCheckOutcome:
        """Measure a representative indexed read against the series registry."""
        return await check_database_query_latency(session, perf_counter=time.perf_counter)

    async def _check_db_size(self, session: AsyncSession) -> SubCheckOutcome | None:
        """Check SQLite database file size."""
        return await check_db_size(session)

    async def _check_db_integrity(self, session: AsyncSession) -> SubCheckOutcome | None:
        """Run SQLite quick_check when supported."""
        return await check_db_integrity(session)

    async def _check_db_bloat(self, session: AsyncSession) -> SubCheckOutcome | None:
        """Estimate SQLite bloat from free-list pages."""
        return await check_db_bloat(session)

    async def _check_filesystem(
        self,
        session: AsyncSession,
    ) -> CheckOutcome:
        """Check operational filesystem targets and persist path-level sub-checks."""
        return await check_filesystem(
            session,
            settings=self._settings,
            check_target=self._check_filesystem_target,
        )

    @staticmethod
    def _check_filesystem_target(
        path: Path,
        name: str,
        require_write: bool,
    ) -> tuple[SubCheckOutcome, str]:
        """Check one operational filesystem target and return a persistable sub-check."""
        return check_filesystem_target(
            path,
            name,
            require_write,
            perf_counter=time.perf_counter,
            scandir=os.scandir,
            mkstemp=tempfile.mkstemp,
            close=os.close,
            unlink=os.unlink,
        )

    async def _check_comicvine(self) -> CheckOutcome:
        """Verify ComicVine auth, connectivity, latency, and rate-limit state."""
        return await check_comicvine(self._registry)

    async def _check_download_clients(self, session: AsyncSession) -> list[CheckOutcome]:
        """Test download clients as a grouped multi-entity health component."""
        return await check_download_clients(
            session,
            registry=self._registry,
            bootstrap_errors=self._bootstrap_errors,
            check_subject=self._check_download_client_subject,
            bootstrap_outcome=self._download_client_bootstrap_outcome,
            unknown_outcome=lambda config: self._download_client_unknown_outcome(
                config,
                message="Waiting for the next client health check.",
            ),
        )

    async def _check_download_client_subject(
        self,
        config: DownloadClientConfig,
        client: DownloadClient,
    ) -> CheckOutcome:
        """Build a persisted health summary for one download client."""
        return await check_download_client_subject(
            config,
            client,
            perf_counter=time.perf_counter,
        )

    def _download_client_bootstrap_outcome(
        self,
        config: DownloadClientConfig,
        bootstrap_error: Mapping[str, str],
    ) -> CheckOutcome:
        """Return a structured subject outcome for a client that could not load."""
        return download_client_bootstrap_outcome(config, bootstrap_error)

    def _download_client_unknown_outcome(
        self,
        config: DownloadClientConfig,
        *,
        message: str,
    ) -> CheckOutcome:
        """Return a placeholder client outcome when no live result exists yet."""
        return download_client_unknown_outcome(config, message=message)

    async def _check_indexers(self, session: AsyncSession) -> list[CheckOutcome]:
        """Test configured search managers and enabled indexers as one component."""
        return await check_indexers(
            session,
            load_prowlarr_subject_config=self._load_prowlarr_subject_config,
            load_jackett_subject_config=self._load_jackett_subject_config,
            check_prowlarr_subject=self._check_prowlarr_subject,
            check_jackett_subject=self._check_jackett_subject,
            check_indexer_subject=self._check_indexer_subject,
        )

    async def _load_prowlarr_subject_config(
        self,
        session: AsyncSession,
    ) -> tuple[str | None, str | None]:
        """Load and decrypt Prowlarr connection settings for health checks."""
        return await load_prowlarr_subject_config(session)

    async def _load_jackett_subject_config(
        self,
        session: AsyncSession,
    ) -> tuple[str | None, str | None]:
        """Load and decrypt Jackett connection settings for health checks."""
        return await load_jackett_subject_config(session)

    async def _check_prowlarr_subject(
        self,
        *,
        url: str,
        api_key: str,
    ) -> CheckOutcome:
        """Build a persisted health summary for the configured Prowlarr proxy."""
        return await check_prowlarr_subject(url=url, api_key=api_key)

    async def _check_jackett_subject(
        self,
        *,
        url: str,
        api_key: str,
    ) -> CheckOutcome:
        """Build a persisted health summary for the configured Jackett proxy."""
        return await check_jackett_subject(url=url, api_key=api_key)

    async def _check_indexer_subject(
        self,
        config: IndexerConfig,
        resolver_options: Sequence[NativeResolverOption],
    ) -> CheckOutcome:
        """Build a persisted health summary for one configured indexer."""
        return await check_indexer_subject(config, resolver_options)

    async def _check_scheduler(self) -> CheckOutcome:
        """Verify the scheduler is running and tasks are executing cleanly."""
        return await check_scheduler(self._scheduler)

    async def _check_system_resources(self) -> CheckOutcome:
        """Check host-level CPU, memory, swap, and disk pressure."""
        return await check_system_resources(
            self._settings,
            check_cpu=self._check_system_cpu,
            check_memory=self._check_system_memory,
            check_swap=self._check_system_swap,
            check_disk=self._check_system_disk,
        )

    async def _check_system_cpu(self) -> tuple[SubCheckOutcome, str]:
        """Measure sustained CPU pressure using normalized load when available."""
        return await check_system_cpu(
            cpu_count=psutil.cpu_count,
            getloadavg=psutil.getloadavg,
            cpu_percent=psutil.cpu_percent,
            os_cpu_count=os.cpu_count,
        )

    async def _check_system_memory(self) -> tuple[SubCheckOutcome, str]:
        """Measure memory pressure using percent used and available RAM."""
        return await check_system_memory(virtual_memory=psutil.virtual_memory)

    async def _check_system_swap(self) -> tuple[SubCheckOutcome, str]:
        """Measure swap activity as a supporting memory-pressure signal."""
        return await check_system_swap(swap_memory=psutil.swap_memory)

    async def _check_system_disk(
        self,
        disk_path: Path,
        disk_path_source: str,
    ) -> tuple[SubCheckOutcome, str]:
        """Measure host disk pressure for the active data path."""
        return await check_system_disk(
            disk_path,
            path_source=disk_path_source,
            disk_usage=shutil.disk_usage,
        )

    # ── Private helpers ───────────────────────────────────────────

    async def _safe_run(
        self,
        coro: Awaitable[CheckOutcome | list[CheckOutcome]],
        component: str,
        check_name: str,
        *,
        session: AsyncSession | None = None,
    ) -> list[CheckOutcome]:
        """Run a check coroutine with timeout and exception isolation."""
        start = time.perf_counter()
        try:
            raw: CheckOutcome | list[CheckOutcome] = await asyncio.wait_for(
                coro,
                timeout=_CHECK_TIMEOUT_SECONDS,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000

            # Normalize to list
            outcomes = raw if isinstance(raw, list) else [raw]

            # Fill in response_time_ms if not already set
            for outcome in outcomes:
                if outcome.response_time_ms == 0.0 and outcome.subject_key is None:
                    outcome.response_time_ms = elapsed_ms

            return outcomes

        except TimeoutError:
            await self._rollback_failed_check_session(session)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("health_check_timeout", component=component, check_name=check_name)
            return [
                CheckOutcome(
                    component=component,
                    check_name=check_name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check timed out after {_CHECK_TIMEOUT_SECONDS}s",
                    response_time_ms=elapsed_ms,
                    actionable_guidance=(
                        f"The {component} health check did not respond within "
                        f"{_CHECK_TIMEOUT_SECONDS} seconds. The service may be "
                        "unresponsive or overloaded."
                    ),
                )
            ]
        except Exception as exc:
            await self._rollback_failed_check_session(session)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "health_check_error", component=component, check_name=check_name, error=str(exc)
            )
            return [
                CheckOutcome(
                    component=component,
                    check_name=check_name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Check failed: {exc}",
                    response_time_ms=elapsed_ms,
                    actionable_guidance=(
                        f"The {component} health check encountered an unexpected error. "
                        "Check application logs for details."
                    ),
                )
            ]

    @staticmethod
    async def _rollback_failed_check_session(session: AsyncSession | None) -> None:
        """Restore a failed check transaction before later checks or persistence."""
        if session is None or session.is_active:
            return
        try:
            await session.rollback()
        except Exception:
            logger.warning("health_check_session_rollback_failed", exc_info=True)

    @staticmethod
    async def _persist_outcomes(
        session: AsyncSession,
        outcomes: list[CheckOutcome],
    ) -> None:
        """Write check outcomes to history and bounded current-state tables."""
        return await persist_health_outcomes(
            session,
            outcomes,
            session_factory_provider=get_session_factory,
            retry_delay=sqlite_lock_retry_delay,
            sleep=asyncio.sleep,
            lock_retry_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
        )

    @staticmethod
    def _stage_outcome_rows(
        session: AsyncSession,
        outcomes: list[CheckOutcome],
        *,
        run_id: str,
        checked_at: datetime,
    ) -> None:
        """Add health outcome rows to the current session transaction."""
        stage_health_outcome_rows(session, outcomes, run_id=run_id, checked_at=checked_at)
