"""Dashboard intelligence service - builds ranked operational briefings."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import ColumnElement, text
from sqlalchemy.exc import DatabaseError, OperationalError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.log_deduper import log_deduped_warning
from pullbox.core.log_sanitizer import sanitize_log_string
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.services import dashboard_helpers as _dashboard_helpers
from pullbox.services.dashboard_metrics import DashboardMetricLoader as _DashboardMetricLoader
from pullbox.services.dashboard_presentation import (
    DashboardPresentationBuilder as _DashboardPresentationBuilder,
)
from pullbox.services.dashboard_priorities import (
    DashboardPriorityBuilder as _DashboardPriorityBuilder,
)
from pullbox.services.dashboard_storage_path import resolve_dashboard_storage_path
from pullbox.services.dashboard_types import (
    ActiveDownloadItem as ActiveDownloadItem,
)
from pullbox.services.dashboard_types import (
    ClientReliabilitySummary as ClientReliabilitySummary,
)
from pullbox.services.dashboard_types import (
    DashboardBriefing as DashboardBriefing,
)
from pullbox.services.dashboard_types import (
    DashboardExceptionItem as DashboardExceptionItem,
)
from pullbox.services.dashboard_types import (
    DashboardIntelligence as DashboardIntelligence,
)
from pullbox.services.dashboard_types import (
    DashboardLivePulse as DashboardLivePulse,
)
from pullbox.services.dashboard_types import (
    DashboardPriority as DashboardPriority,
)
from pullbox.services.dashboard_types import (
    DashboardScorecard as DashboardScorecard,
)
from pullbox.services.dashboard_types import (
    DashboardSnapshot as DashboardSnapshot,
)
from pullbox.services.dashboard_types import (
    DashboardWatchItem as DashboardWatchItem,
)
from pullbox.services.dashboard_types import (
    DownloadSummary as DownloadSummary,
)
from pullbox.services.dashboard_types import (
    FailureCluster as FailureCluster,
)
from pullbox.services.dashboard_types import (
    HealthSummary as HealthSummary,
)
from pullbox.services.dashboard_types import (
    ImportFailureSummary as ImportFailureSummary,
)
from pullbox.services.dashboard_types import (
    ReleaseRiskSummary as ReleaseRiskSummary,
)
from pullbox.services.dashboard_types import (
    ReviewDebtSummary as ReviewDebtSummary,
)
from pullbox.services.dashboard_types import (
    SearchYieldSummary as SearchYieldSummary,
)
from pullbox.services.dashboard_types import (
    StorageSummary as StorageSummary,
)

_ACTIVE_DOWNLOAD_STATES = (
    DownloadState.QUEUED,
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
)
_SUCCESS_DOWNLOAD_STATES = (DownloadState.COMPLETED, DownloadState.IMPORTED)
_ROLLUP_KEYS = (
    "active_downloads",
    "review_debt_total",
    "release_risk_count",
    "flow_through_rate",
    "client_reliability_rate",
    "storage_used_percent",
    "search_yield_rate",
    "import_failure_count",
    "unmatched_backlog",
    "health_problem_count",
)

logger = structlog.get_logger(__name__)

_CACHE_WRITE_RETRY_ATTEMPTS = 2


class DashboardIntelligenceService:
    """Build the dashboard as a ranked operations briefing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _metric_loader(self) -> _DashboardMetricLoader:
        """Build the SQL-backed dashboard metric loader with legacy patch seams."""
        return _DashboardMetricLoader(
            self._session,
            rollback_after_cache_error=self._rollback_after_cache_error,
            log_cache_error=self._log_cache_error,
            run_best_effort_cache_write=self._run_best_effort_cache_write,
            disk_usage_func=shutil.disk_usage,
            resolve_storage_path_func=lambda: resolve_dashboard_storage_path(self._session),
        )

    def _priority_builder(self) -> _DashboardPriorityBuilder:
        """Build the dashboard priority assembler."""
        return _DashboardPriorityBuilder()

    def _presentation_builder(self) -> _DashboardPresentationBuilder:
        """Build the dashboard presentation assembler."""
        return _DashboardPresentationBuilder()

    async def _rollback_after_cache_error(self) -> None:
        """Reset the session after a non-fatal dashboard cache failure."""
        try:
            await self._session.rollback()
        except Exception:
            logger.warning("dashboard_cache_rollback_failed", exc_info=True)

    def _log_cache_error(self, *, cache_key: str, exc: Exception) -> None:
        """Emit one deduplicated warning when a derived dashboard cache is unavailable."""
        log_deduped_warning(
            logger,
            "dashboard_cache_unavailable",
            key=("dashboard_cache_unavailable", cache_key, type(exc).__name__),
            window_seconds=300.0,
            cache_key=cache_key,
            action_required=(
                "Pullbox will continue with live dashboard calculations and without this cache."
            ),
            error=sanitize_log_string(str(exc)),
        )

    @staticmethod
    def _is_locked_error(exc: BaseException) -> bool:
        """Return True when SQLite reports a transient lock."""
        message = str(exc).lower()
        return "database is locked" in message or "locking protocol" in message

    async def _run_best_effort_cache_write(
        self,
        *,
        cache_key: str,
        writer: Callable[[AsyncSession], Awaitable[None]],
    ) -> None:
        """Persist dashboard cache state with a dedicated short-timeout session."""
        from pullbox.database import get_session_factory

        factory = get_session_factory()
        for attempt in range(_CACHE_WRITE_RETRY_ATTEMPTS):
            async with factory() as session:
                try:
                    dialect = session.get_bind().dialect.name
                    if dialect == "sqlite":
                        await session.execute(text("PRAGMA busy_timeout = 1000"))
                    elif dialect == "postgresql":
                        await session.execute(text("SET LOCAL lock_timeout = '1s'"))
                    await writer(session)
                    await session.commit()
                    return
                except OperationalError as exc:
                    await session.rollback()
                    if self._is_locked_error(exc) and attempt < (_CACHE_WRITE_RETRY_ATTEMPTS - 1):
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    self._log_cache_error(cache_key=cache_key, exc=exc)
                    return
                except DatabaseError as exc:
                    await session.rollback()
                    self._log_cache_error(cache_key=cache_key, exc=exc)
                    return
                except Exception:
                    await session.rollback()
                    logger.warning(
                        "dashboard_cache_write_failed",
                        cache_key=cache_key,
                        exc_info=True,
                    )
                    return

    async def build_dashboard(
        self,
        *,
        now: datetime | None = None,
        allow_rollup_refresh: bool = False,
    ) -> DashboardIntelligence:
        """Build the dashboard intelligence payload for the current request."""
        current_time = now or datetime.now(UTC)
        snapshot = await self._load_snapshot(current_time)

        if allow_rollup_refresh:
            await self._ensure_daily_storage_snapshot(snapshot.storage, current_time)
            if await self._should_refresh_rollups(snapshot, current_time):
                await self._persist_rollups(snapshot, current_time)
                latest_rollup_at = _hour_bucket_start(current_time)
                snapshot = DashboardSnapshot(
                    computed_at=snapshot.computed_at,
                    latest_rollup_at=latest_rollup_at,
                    downloads=snapshot.downloads,
                    client_reliability=snapshot.client_reliability,
                    review_debt=snapshot.review_debt,
                    release_risk=snapshot.release_risk,
                    search_yield=snapshot.search_yield,
                    import_failures=snapshot.import_failures,
                    health=snapshot.health,
                    storage=snapshot.storage,
                    failure_clusters=snapshot.failure_clusters,
                    unmatched_clusters=snapshot.unmatched_clusters,
                )

        priorities = self._build_priorities(snapshot)
        briefing = self._build_briefing(snapshot, priorities)
        is_first_run = (
            snapshot.latest_rollup_at is None and not await self._has_terminal_download_history()
        )

        return DashboardIntelligence(
            briefing=briefing,
            priorities=tuple(priorities[:5]),
            scorecards=self._build_scorecards(snapshot),
            watch_items=self._build_watch_items(snapshot),
            exceptions=self._build_exceptions(snapshot),
            freshness=snapshot.computed_at,
            live_pulse=self._build_live_pulse(snapshot),
            is_first_run=is_first_run,
        )

    async def capture_rollups(self, *, now: datetime | None = None) -> None:
        """Recompute and persist the current dashboard rollups."""
        current_time = now or datetime.now(UTC)
        snapshot = await self._load_snapshot(current_time)
        await self._ensure_daily_storage_snapshot(snapshot.storage, current_time)
        await self._persist_rollups(snapshot, current_time)

    async def _load_snapshot(self, current_time: datetime) -> DashboardSnapshot:
        """Compatibility facade for SQL-backed dashboard snapshot loading."""
        return await self._metric_loader().load_snapshot(current_time)

    async def _load_reference_metrics(self, current_time: datetime) -> dict[str, float]:
        """Compatibility facade for dashboard trend-baseline rollups."""
        return await self._metric_loader().load_reference_metrics(current_time)

    async def _latest_rollup_timestamp(self) -> datetime | None:
        """Compatibility facade for latest dashboard rollup timestamp loading."""
        return await self._metric_loader().latest_rollup_timestamp()

    async def _has_terminal_download_history(self) -> bool:
        """Compatibility facade for first-run download-history detection."""
        return await self._metric_loader().has_terminal_download_history()

    async def _load_download_summary(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> DownloadSummary:
        """Compatibility facade for download funnel metrics."""
        return await self._metric_loader().load_download_summary(
            window_start,
            previous_start,
            current_time,
        )

    async def _load_client_reliability(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> ClientReliabilitySummary:
        """Compatibility facade for download-client reliability metrics."""
        return await self._metric_loader().load_client_reliability(
            window_start,
            previous_start,
            current_time,
        )

    async def _load_client_reliability_window(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[str, int, int]]:
        """Compatibility facade for per-client reliability windows."""
        return await self._metric_loader().load_client_reliability_window(start, end)

    async def _load_review_debt(self, reference_total: float | None) -> ReviewDebtSummary:
        """Compatibility facade for review-debt metric loading."""
        return await self._metric_loader().load_review_debt(reference_total)

    async def _load_release_risk(
        self,
        today: date,
        reference_count: float | None,
    ) -> ReleaseRiskSummary:
        """Compatibility facade for release-risk metric loading."""
        return await self._metric_loader().load_release_risk(today, reference_count)

    async def _load_search_yield(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> SearchYieldSummary:
        """Compatibility facade for search-yield metric loading."""
        return await self._metric_loader().load_search_yield(
            window_start,
            previous_start,
            current_time,
        )

    async def _load_import_failures(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> ImportFailureSummary:
        """Compatibility facade for import-failure metric loading."""
        return await self._metric_loader().load_import_failures(
            window_start,
            previous_start,
            current_time,
        )

    async def _load_health_summary(self, reference_problem_count: float | None) -> HealthSummary:
        """Compatibility facade for health summary metric loading."""
        return await self._metric_loader().load_health_summary(reference_problem_count)

    async def _load_storage_summary(self, current_time: datetime) -> StorageSummary:
        """Compatibility facade for storage summary metric loading."""
        return await self._metric_loader().load_storage_summary(current_time)

    async def _load_failure_clusters(
        self,
        window_start: datetime,
        current_time: datetime,
    ) -> tuple[FailureCluster, ...]:
        """Compatibility facade for repeated download-failure clusters."""
        return await self._metric_loader().load_failure_clusters(window_start, current_time)

    async def _load_unmatched_clusters(self) -> tuple[FailureCluster, ...]:
        """Compatibility facade for repeated unmatched-file clusters."""
        return await self._metric_loader().load_unmatched_clusters()

    async def _ensure_daily_storage_snapshot(
        self,
        storage: StorageSummary,
        current_time: datetime,
    ) -> None:
        """Compatibility facade for daily storage snapshot persistence."""
        await self._metric_loader().ensure_daily_storage_snapshot(storage, current_time)

    async def _should_refresh_rollups(
        self,
        snapshot: DashboardSnapshot,
        current_time: datetime,
    ) -> bool:
        """Compatibility facade for dashboard rollup freshness checks."""
        return await self._metric_loader().should_refresh_rollups(snapshot, current_time)

    async def _persist_rollups(self, snapshot: DashboardSnapshot, current_time: datetime) -> None:
        """Compatibility facade for dashboard rollup persistence."""
        await self._metric_loader().persist_rollups(snapshot, current_time)

    def _build_briefing(
        self,
        snapshot: DashboardSnapshot,
        priorities: list[DashboardPriority],
    ) -> DashboardBriefing:
        """Compatibility facade for dashboard briefing assembly."""
        return self._presentation_builder().build_briefing(snapshot, priorities)

    def _build_scorecards(self, snapshot: DashboardSnapshot) -> tuple[DashboardScorecard, ...]:
        """Compatibility facade for dashboard scorecard assembly."""
        return self._presentation_builder().build_scorecards(snapshot)

    def _build_watch_items(self, snapshot: DashboardSnapshot) -> tuple[DashboardWatchItem, ...]:
        """Compatibility facade for dashboard watch-item assembly."""
        return self._presentation_builder().build_watch_items(snapshot)

    def _build_exceptions(self, snapshot: DashboardSnapshot) -> tuple[DashboardExceptionItem, ...]:
        """Compatibility facade for dashboard exception assembly."""
        return self._presentation_builder().build_exceptions(snapshot)

    def _build_live_pulse(self, snapshot: DashboardSnapshot) -> DashboardLivePulse:
        """Compatibility facade for dashboard live-pulse assembly."""
        return self._presentation_builder().build_live_pulse(snapshot)

    def _build_priorities(self, snapshot: DashboardSnapshot) -> list[DashboardPriority]:
        """Compatibility facade for dashboard priority ranking."""
        return self._priority_builder().build_priorities(snapshot)

    def _build_health_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        """Compatibility facade for health priority assembly."""
        return self._priority_builder().build_health_priority(snapshot)

    def _build_storage_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        """Compatibility facade for storage priority assembly."""
        return self._priority_builder().build_storage_priority(snapshot)

    def _build_client_failure_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        """Compatibility facade for client-failure priority assembly."""
        return self._priority_builder().build_client_failure_priority(snapshot)

    def _build_review_debt_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        """Compatibility facade for review-debt priority assembly."""
        return self._priority_builder().build_review_debt_priority(snapshot)

    def _build_release_risk_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        """Compatibility facade for release-risk priority assembly."""
        return self._priority_builder().build_release_risk_priority(snapshot)

    def _build_search_yield_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        """Compatibility facade for search-yield priority assembly."""
        return self._priority_builder().build_search_yield_priority(snapshot)

    def _build_import_failure_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        """Compatibility facade for import-failure priority assembly."""
        return self._priority_builder().build_import_failure_priority(snapshot)

    def _build_unmatched_growth_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        """Compatibility facade for unmatched-growth priority assembly."""
        return self._priority_builder().build_unmatched_growth_priority(snapshot)


def _download_failure_clause() -> ColumnElement[bool]:
    return (DownloadHistory.state == DownloadState.FAILED) & (
        (DownloadHistory.error_message.is_(None))
        | (DownloadHistory.error_message != "Cancelled by user")
    )


def _download_client_label(client_type: DownloadClientType | str) -> str:
    value = client_type.value if isinstance(client_type, DownloadClientType) else client_type
    labels = {
        DownloadClientType.SABNZBD.value: "SABnzbd",
        DownloadClientType.NZBGET.value: "NZBGet",
        DownloadClientType.QBITTORRENT.value: "qBittorrent",
        DownloadClientType.TRANSMISSION.value: "Transmission",
        DownloadClientType.DELUGE.value: "Deluge",
    }
    return labels.get(value, value.replace("_", " ").title())


def _hour_bucket_start(current_time: datetime) -> datetime:
    return current_time.replace(minute=0, second=0, microsecond=0)


_safe_percent = _dashboard_helpers.safe_percent
_priority_score = _dashboard_helpers.priority_score
_priority_state = _dashboard_helpers.priority_state
_priority_state_label = _dashboard_helpers.priority_state_label
_trend_score = _dashboard_helpers.trend_score
_rate_drop_score = _dashboard_helpers.rate_drop_score
_rate_state_from_percent = _dashboard_helpers.rate_state_from_percent
_count_state = _dashboard_helpers.count_state
_format_percent_label = _dashboard_helpers.format_percent_label
_format_rate_delta = _dashboard_helpers.format_rate_delta
_delta_from_reference = _dashboard_helpers.delta_from_reference
_format_count_delta = _dashboard_helpers.format_count_delta
_trend_label = _dashboard_helpers.trend_label
_flow_through_interpretation = _dashboard_helpers.flow_through_interpretation
_review_debt_interpretation = _dashboard_helpers.review_debt_interpretation
_release_risk_interpretation = _dashboard_helpers.release_risk_interpretation
_client_reliability_interpretation = _dashboard_helpers.client_reliability_interpretation
_storage_runway_label = _dashboard_helpers.storage_runway_label
_storage_growth_label = _dashboard_helpers.storage_growth_label
_storage_interpretation = _dashboard_helpers.storage_interpretation
_storage_state_from_percent = _dashboard_helpers.storage_state_from_percent
_storage_growth_rates = _dashboard_helpers.storage_growth_rates
_project_days_remaining = _dashboard_helpers.project_days_remaining
_storage_is_accelerating = _dashboard_helpers.storage_is_accelerating
_storage_trend_score = _dashboard_helpers.storage_trend_score
_count_label = _dashboard_helpers.count_label
_storage_severity = _dashboard_helpers.storage_severity
_storage_imminence = _dashboard_helpers.storage_imminence
_search_yield_is_drifting = _dashboard_helpers.search_yield_is_drifting
_age_in_days = _dashboard_helpers.age_in_days
_oldest_age_label = _dashboard_helpers.oldest_age_label
_days_until = _dashboard_helpers.days_until
_release_time_label = _dashboard_helpers.release_time_label
