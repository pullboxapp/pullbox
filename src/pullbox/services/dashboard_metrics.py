"""SQL-backed dashboard metric loading and rollup persistence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import DatabaseError, OperationalError

from pullbox.models.dashboard import DashboardMetricRollup, DashboardStorageSnapshot
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.health import HealthCurrentStatus, HealthStatus
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.search_log import SearchLog
from pullbox.services.dashboard_helpers import (
    safe_percent,
)
from pullbox.services.dashboard_metric_rollups import (
    DASHBOARD_ROLLUP_KEYS as _ROLLUP_KEYS,
)
from pullbox.services.dashboard_metric_rollups import (
    dashboard_download_client_label as _download_client_label,
)
from pullbox.services.dashboard_metric_rollups import (
    dashboard_hour_bucket_start as _hour_bucket_start,
)
from pullbox.services.dashboard_metric_rollups import dashboard_rollup_payload
from pullbox.services.dashboard_storage_summary import build_dashboard_storage_summary
from pullbox.services.dashboard_types import (
    ClientReliabilitySummary,
    DashboardSnapshot,
    DownloadSummary,
    FailureCluster,
    HealthSummary,
    ImportFailureSummary,
    ReleaseRiskSummary,
    ReviewDebtSummary,
    SearchYieldSummary,
    StorageSummary,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from os import PathLike
    from pathlib import Path

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    DiskUsageFunc = Callable[
        [int | str | bytes | PathLike[str] | PathLike[bytes]],
        object,
    ]


_ACTIVE_DOWNLOAD_STATES = (
    DownloadState.QUEUED,
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
)
_SUCCESS_DOWNLOAD_STATES = (DownloadState.COMPLETED, DownloadState.IMPORTED)


class DiskUsageResult(Protocol):
    """Return shape from disk-usage providers."""

    total: int
    used: int
    free: int


class _ZeroDiskUsage:
    """Fallback usage when the intended dashboard storage path is unavailable."""

    total = 0
    used = 0
    free = 0


class LogCacheErrorFunc(Protocol):
    """Callable shape for dashboard cache warning hooks."""

    def __call__(self, *, cache_key: str, exc: Exception) -> None: ...


class CacheWriteFunc(Protocol):
    """Callable shape for best-effort dashboard cache writes."""

    def __call__(
        self,
        *,
        cache_key: str,
        writer: Callable[[AsyncSession], Awaitable[None]],
    ) -> Awaitable[None]: ...


def _download_failure_clause() -> ColumnElement[bool]:
    return (DownloadHistory.state == DownloadState.FAILED) & (
        (DownloadHistory.error_message.is_(None))
        | (DownloadHistory.error_message != "Cancelled by user")
    )


class DashboardMetricLoader:
    """Load dashboard metric snapshots and persist rollup cache rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        rollback_after_cache_error: Callable[[], Awaitable[None]],
        log_cache_error: LogCacheErrorFunc,
        run_best_effort_cache_write: CacheWriteFunc,
        disk_usage_func: DiskUsageFunc,
        resolve_storage_path_func: Callable[[], Awaitable[Path]],
    ) -> None:
        self._session = session
        self._rollback_after_cache_error = rollback_after_cache_error
        self._log_cache_error = log_cache_error
        self._run_best_effort_cache_write = run_best_effort_cache_write
        self._disk_usage_func = disk_usage_func
        self._resolve_storage_path_func = resolve_storage_path_func

    async def load_snapshot(self, current_time: datetime) -> DashboardSnapshot:
        """Load the full SQL-backed snapshot used to assemble dashboard cards."""
        window_start = current_time - timedelta(days=7)
        previous_start = current_time - timedelta(days=14)
        latest_rollup_at = await self.latest_rollup_timestamp()
        reference_metrics = await self.load_reference_metrics(current_time)

        downloads = await self.load_download_summary(window_start, previous_start, current_time)
        client_reliability = await self.load_client_reliability(
            window_start,
            previous_start,
            current_time,
        )
        review_debt = await self.load_review_debt(reference_metrics.get("review_debt_total"))
        release_risk = await self.load_release_risk(
            current_time.date(),
            reference_metrics.get("release_risk_count"),
        )
        search_yield = await self.load_search_yield(window_start, previous_start, current_time)
        import_failures = await self.load_import_failures(
            window_start,
            previous_start,
            current_time,
        )
        health = await self.load_health_summary(reference_metrics.get("health_problem_count"))
        storage = await self.load_storage_summary(current_time)
        failure_clusters = await self.load_failure_clusters(window_start, current_time)
        unmatched_clusters = await self.load_unmatched_clusters()

        return DashboardSnapshot(
            computed_at=current_time,
            latest_rollup_at=latest_rollup_at,
            downloads=downloads,
            client_reliability=client_reliability,
            review_debt=review_debt,
            release_risk=release_risk,
            search_yield=search_yield,
            import_failures=import_failures,
            health=health,
            storage=storage,
            failure_clusters=failure_clusters,
            unmatched_clusters=unmatched_clusters,
        )

    async def load_reference_metrics(self, current_time: datetime) -> dict[str, float]:
        """Load previous-period rollups used as trend baselines."""
        target_time = current_time - timedelta(days=7)
        values: dict[str, float] = {}
        try:
            for metric_key in (
                "review_debt_total",
                "release_risk_count",
                "unmatched_backlog",
                "health_problem_count",
            ):
                result = await self._session.execute(
                    select(DashboardMetricRollup.value)
                    .where(
                        DashboardMetricRollup.metric_key == metric_key,
                        DashboardMetricRollup.bucket_start <= target_time,
                    )
                    .order_by(DashboardMetricRollup.bucket_start.desc())
                    .limit(1)
                )
                value = result.scalar_one_or_none()
                if value is not None:
                    values[metric_key] = float(value)
        except (OperationalError, DatabaseError) as exc:
            await self._rollback_after_cache_error()
            self._log_cache_error(cache_key="dashboard_metric_rollups", exc=exc)
        return values

    async def latest_rollup_timestamp(self) -> datetime | None:
        """Load the latest persisted dashboard rollup timestamp."""
        try:
            result = await self._session.execute(
                select(func.max(DashboardMetricRollup.bucket_start))
            )
            return result.scalar_one_or_none()
        except (OperationalError, DatabaseError) as exc:
            await self._rollback_after_cache_error()
            self._log_cache_error(cache_key="dashboard_metric_rollups", exc=exc)
            return None

    async def has_terminal_download_history(self) -> bool:
        """Return whether any completed/imported/failed download history exists."""
        result = await self._session.execute(
            select(func.count(DownloadHistory.id)).where(
                or_(
                    DownloadHistory.state.in_(_SUCCESS_DOWNLOAD_STATES),
                    DownloadHistory.imported_at.is_not(None),
                    _download_failure_clause(),
                )
            )
        )
        return int(result.scalar_one() or 0) > 0

    async def load_download_summary(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> DownloadSummary:
        """Load download funnel metrics for the current and previous windows."""
        time_expr = func.coalesce(
            DownloadHistory.imported_at,
            DownloadHistory.completed_at,
            DownloadHistory.updated_at,
        )
        success_clause = or_(
            DownloadHistory.state.in_(_SUCCESS_DOWNLOAD_STATES),
            DownloadHistory.imported_at.is_not(None),
        )
        imported_clause = or_(
            DownloadHistory.state == DownloadState.IMPORTED,
            DownloadHistory.imported_at.is_not(None),
        )
        failure_clause = _download_failure_clause()
        terminal_clause = or_(success_clause, failure_clause)

        active_count = int(
            (
                await self._session.execute(
                    select(func.count(DownloadHistory.id)).where(
                        DownloadHistory.state.in_(_ACTIVE_DOWNLOAD_STATES)
                    )
                )
            ).scalar_one()
        )

        current_row = (
            await self._session.execute(
                select(
                    func.count(DownloadHistory.id),
                    func.coalesce(func.sum(case((imported_clause, 1), else_=0)), 0),
                ).where(
                    terminal_clause,
                    time_expr >= window_start,
                    time_expr < current_time,
                )
            )
        ).one()
        previous_row = (
            await self._session.execute(
                select(
                    func.count(DownloadHistory.id),
                    func.coalesce(func.sum(case((imported_clause, 1), else_=0)), 0),
                ).where(
                    terminal_clause,
                    time_expr >= previous_start,
                    time_expr < window_start,
                )
            )
        ).one()

        return DownloadSummary(
            active_count=active_count,
            terminal_count=int(current_row[0] or 0),
            imported_count=int(current_row[1] or 0),
            previous_terminal_count=int(previous_row[0] or 0),
            previous_imported_count=int(previous_row[1] or 0),
        )

    async def load_client_reliability(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> ClientReliabilitySummary:
        """Load current and previous download-client reliability metrics."""
        current_stats = await self.load_client_reliability_window(window_start, current_time)
        previous_stats = await self.load_client_reliability_window(previous_start, window_start)

        current_total = sum(total for _, total, _ in current_stats)
        current_successes = sum(successes for _, _, successes in current_stats)
        previous_total = sum(total for _, total, _ in previous_stats)
        previous_successes = sum(successes for _, _, successes in previous_stats)

        worst_client_label: str | None = None
        worst_client_rate: float | None = None
        worst_client_failures = 0
        for client_label, total, successes in current_stats:
            if total <= 0:
                continue
            rate = (successes / total) * 100.0
            failures = total - successes
            if worst_client_rate is None or rate < worst_client_rate:
                worst_client_label = client_label
                worst_client_rate = rate
                worst_client_failures = failures

        return ClientReliabilitySummary(
            rate=safe_percent(current_successes, current_total),
            previous_rate=safe_percent(previous_successes, previous_total),
            worst_client_label=worst_client_label,
            worst_client_rate=worst_client_rate,
            worst_client_failures=worst_client_failures,
        )

    async def load_client_reliability_window(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[str, int, int]]:
        """Load per-client terminal download success counts for a window."""
        time_expr = func.coalesce(
            DownloadHistory.imported_at,
            DownloadHistory.completed_at,
            DownloadHistory.updated_at,
        )
        success_clause = or_(
            DownloadHistory.state.in_(_SUCCESS_DOWNLOAD_STATES),
            DownloadHistory.imported_at.is_not(None),
        )
        failure_clause = _download_failure_clause()
        terminal_clause = or_(success_clause, failure_clause)
        rows = (
            await self._session.execute(
                select(
                    DownloadHistory.download_client,
                    func.count(DownloadHistory.id),
                    func.coalesce(func.sum(case((success_clause, 1), else_=0)), 0),
                )
                .where(
                    terminal_clause,
                    time_expr >= start,
                    time_expr < end,
                )
                .group_by(DownloadHistory.download_client)
            )
        ).all()

        result: list[tuple[str, int, int]] = []
        for client, total, successes in rows:
            if client is None:
                continue
            result.append((_download_client_label(client), int(total or 0), int(successes or 0)))
        return result

    async def load_review_debt(self, reference_total: float | None) -> ReviewDebtSummary:
        """Load pending manual review, suggestion, and unmatched-file debt."""
        pending_count = int(
            (
                await self._session.execute(
                    select(func.count(PendingMatch.id)).where(
                        PendingMatch.status == PendingMatchStatus.PENDING
                    )
                )
            ).scalar_one()
        )
        pending_oldest = (
            await self._session.execute(
                select(func.min(PendingMatch.created_at)).where(
                    PendingMatch.status == PendingMatchStatus.PENDING
                )
            )
        ).scalar_one_or_none()

        suggestion_count = int(
            (
                await self._session.execute(
                    select(func.count(MatchingSuggestion.id)).where(
                        MatchingSuggestion.status == SuggestionStatus.PENDING
                    )
                )
            ).scalar_one()
        )
        suggestion_oldest = (
            await self._session.execute(
                select(func.min(MatchingSuggestion.created_at)).where(
                    MatchingSuggestion.status == SuggestionStatus.PENDING
                )
            )
        ).scalar_one_or_none()

        unmatched_count = int(
            (
                await self._session.execute(
                    select(func.count(LibraryFile.id)).where(
                        LibraryFile.match_confidence == MatchConfidence.UNMATCHED
                    )
                )
            ).scalar_one()
        )
        unmatched_oldest = (
            await self._session.execute(
                select(func.min(LibraryFile.created_at)).where(
                    LibraryFile.match_confidence == MatchConfidence.UNMATCHED
                )
            )
        ).scalar_one_or_none()

        oldest_candidates = [
            dt for dt in (pending_oldest, suggestion_oldest, unmatched_oldest) if dt
        ]
        oldest_at = min(oldest_candidates) if oldest_candidates else None

        return ReviewDebtSummary(
            pending_matches=pending_count,
            suggestions=suggestion_count,
            unmatched_backlog=unmatched_count,
            total=pending_count + suggestion_count + unmatched_count,
            oldest_at=oldest_at,
            reference_total=reference_total,
        )

    async def load_release_risk(
        self,
        today: date,
        reference_count: float | None,
    ) -> ReleaseRiskSummary:
        """Load upcoming wanted/download-risk issue counts."""
        next_72h = today + timedelta(days=3)
        next_7d = today + timedelta(days=7)
        at_risk_clause = Issue.status.notin_((IssueStatus.OWNED, IssueStatus.DOWNLOADING))

        next_72h_count = int(
            (
                await self._session.execute(
                    select(func.count(Issue.id)).where(
                        Issue.release_date.is_not(None),
                        Issue.release_date >= today,
                        Issue.release_date <= next_72h,
                        at_risk_clause,
                    )
                )
            ).scalar_one()
        )
        next_7d_count = int(
            (
                await self._session.execute(
                    select(func.count(Issue.id)).where(
                        Issue.release_date.is_not(None),
                        Issue.release_date >= today,
                        Issue.release_date <= next_7d,
                        at_risk_clause,
                    )
                )
            ).scalar_one()
        )
        nearest_release_date = (
            await self._session.execute(
                select(func.min(Issue.release_date)).where(
                    Issue.release_date.is_not(None),
                    Issue.release_date >= today,
                    Issue.release_date <= next_72h,
                    at_risk_clause,
                )
            )
        ).scalar_one_or_none()

        return ReleaseRiskSummary(
            next_72h_count=next_72h_count,
            next_7d_count=next_7d_count,
            nearest_release_date=nearest_release_date,
            reference_count=reference_count,
        )

    async def load_search_yield(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> SearchYieldSummary:
        """Load search success/yield metrics for current and previous windows."""
        current_row = (
            await self._session.execute(
                select(
                    func.count(SearchLog.id),
                    func.coalesce(
                        func.sum(SearchLog.results_grabbed + SearchLog.results_queued),
                        0,
                    ),
                    func.coalesce(func.sum(SearchLog.results_found), 0),
                ).where(SearchLog.created_at >= window_start, SearchLog.created_at < current_time)
            )
        ).one()
        previous_row = (
            await self._session.execute(
                select(
                    func.count(SearchLog.id),
                    func.coalesce(
                        func.sum(SearchLog.results_grabbed + SearchLog.results_queued),
                        0,
                    ),
                    func.coalesce(func.sum(SearchLog.results_found), 0),
                ).where(SearchLog.created_at >= previous_start, SearchLog.created_at < window_start)
            )
        ).one()

        current_matched = int(current_row[1] or 0)
        current_found = int(current_row[2] or 0)
        previous_matched = int(previous_row[1] or 0)
        previous_found = int(previous_row[2] or 0)

        return SearchYieldSummary(
            searches=int(current_row[0] or 0),
            matched_results=current_matched,
            rate=safe_percent(current_matched, current_found),
            previous_searches=int(previous_row[0] or 0),
            previous_matched_results=previous_matched,
            previous_rate=safe_percent(previous_matched, previous_found),
        )

    async def load_import_failures(
        self,
        window_start: datetime,
        previous_start: datetime,
        current_time: datetime,
    ) -> ImportFailureSummary:
        """Load failed/cancelled import counts for current and previous windows."""
        current_row = (
            await self._session.execute(
                select(
                    func.count(ImportJob.id),
                    func.coalesce(func.sum(ImportJob.total_files_failed), 0),
                ).where(
                    ImportJob.updated_at >= window_start,
                    ImportJob.updated_at < current_time,
                    ImportJob.status.in_((ImportJobStatus.FAILED, ImportJobStatus.CANCELLED)),
                )
            )
        ).one()
        previous_row = (
            await self._session.execute(
                select(
                    func.count(ImportJob.id),
                    func.coalesce(func.sum(ImportJob.total_files_failed), 0),
                ).where(
                    ImportJob.updated_at >= previous_start,
                    ImportJob.updated_at < window_start,
                    ImportJob.status.in_((ImportJobStatus.FAILED, ImportJobStatus.CANCELLED)),
                )
            )
        ).one()

        return ImportFailureSummary(
            failed_jobs=int(current_row[0] or 0),
            failed_files=int(current_row[1] or 0),
            previous_failed_jobs=int(previous_row[0] or 0),
            previous_failed_files=int(previous_row[1] or 0),
        )

    async def load_health_summary(self, reference_problem_count: float | None) -> HealthSummary:
        """Load current summary health status counts."""
        rows = (
            await self._session.execute(
                select(
                    HealthCurrentStatus.component,
                    HealthCurrentStatus.status,
                ).where(
                    HealthCurrentStatus.is_summary.is_(True),
                    HealthCurrentStatus.subject_key_norm == "",
                )
            )
        ).all()

        degraded_count = 0
        unhealthy_count = 0
        component_labels: list[str] = []
        for component, status in rows:
            if status == HealthStatus.DEGRADED:
                degraded_count += 1
                component_labels.append(f"{str(component).replace('_', ' ').title()} degraded")
            elif status == HealthStatus.UNHEALTHY:
                unhealthy_count += 1
                component_labels.append(f"{str(component).replace('_', ' ').title()} failing")

        return HealthSummary(
            degraded_count=degraded_count,
            unhealthy_count=unhealthy_count,
            component_labels=tuple(component_labels[:3]),
            reference_problem_count=reference_problem_count,
        )

    async def load_storage_summary(self, current_time: datetime) -> StorageSummary:
        """Load disk usage and storage-growth summary metrics."""
        storage_path = await self._resolve_storage_path_func()
        try:
            usage = cast("DiskUsageResult", self._disk_usage_func(storage_path))
        except OSError:
            usage = _ZeroDiskUsage()

        try:
            snapshots = (
                (
                    await self._session.execute(
                        select(DashboardStorageSnapshot)
                        .where(DashboardStorageSnapshot.snapshot_date <= current_time.date())
                        .order_by(DashboardStorageSnapshot.snapshot_date.desc())
                        .limit(8)
                    )
                )
                .scalars()
                .all()
            )
        except (OperationalError, DatabaseError) as exc:
            await self._rollback_after_cache_error()
            self._log_cache_error(cache_key="dashboard_storage_snapshots", exc=exc)
            snapshots = []
        ordered_snapshots = list(reversed(snapshots))
        return build_dashboard_storage_summary(
            source_path=str(storage_path),
            total_bytes=int(usage.total),
            used_bytes=int(usage.used),
            free_bytes=int(usage.free),
            snapshots=ordered_snapshots,
        )

    async def load_failure_clusters(
        self,
        window_start: datetime,
        current_time: datetime,
    ) -> tuple[FailureCluster, ...]:
        """Load repeated download failure clusters."""
        time_expr = func.coalesce(
            DownloadHistory.completed_at,
            DownloadHistory.updated_at,
        )
        rows = (
            await self._session.execute(
                select(
                    DownloadHistory.download_client,
                    DownloadHistory.error_message,
                    func.count(DownloadHistory.id),
                )
                .where(
                    _download_failure_clause(),
                    time_expr >= window_start,
                    time_expr < current_time,
                )
                .group_by(DownloadHistory.download_client, DownloadHistory.error_message)
                .order_by(func.count(DownloadHistory.id).desc())
                .limit(3)
            )
        ).all()

        clusters: list[FailureCluster] = []
        for idx, row in enumerate(rows, start=1):
            client = row[0]
            if client is None:
                continue
            error_message = str(row[1] or "Unknown failure")
            count = int(row[2] or 0)
            if count <= 1:
                continue
            clusters.append(
                FailureCluster(
                    key=f"download-failure-{idx}",
                    title=f"{_download_client_label(client)} is repeating the same failure",
                    detail=f"{count} recent failures ended with “{error_message}”.",
                    count=count,
                    cta_label="Open downloads",
                    cta_href="/downloads?tab=history",
                    state="critical" if count >= 4 else "watch",
                )
            )
        return tuple(clusters)

    async def load_unmatched_clusters(self) -> tuple[FailureCluster, ...]:
        """Load repeated unmatched parsed-series clusters."""
        rows = (
            await self._session.execute(
                select(
                    LibraryFile.parsed_series,
                    func.count(LibraryFile.id),
                )
                .where(
                    LibraryFile.match_confidence == MatchConfidence.UNMATCHED,
                    LibraryFile.parsed_series.is_not(None),
                )
                .group_by(LibraryFile.parsed_series)
                .order_by(func.count(LibraryFile.id).desc())
                .limit(3)
            )
        ).all()

        clusters: list[FailureCluster] = []
        for idx, row in enumerate(rows, start=1):
            parsed_series = row[0]
            if parsed_series is None:
                continue
            count = int(row[1] or 0)
            if count <= 1:
                continue
            clusters.append(
                FailureCluster(
                    key=f"unmatched-series-{idx}",
                    title=f"{parsed_series} is piling up unmatched files",
                    detail=f"{count} files still need a clean match.",
                    count=count,
                    cta_label="Review unmatched",
                    cta_href="/import?tab=follow-up",
                    state="watch",
                )
            )
        return tuple(clusters)

    async def ensure_daily_storage_snapshot(
        self,
        storage: StorageSummary,
        current_time: datetime,
    ) -> None:
        """Persist the daily storage snapshot cache row."""

        async def _write(session: AsyncSession) -> None:
            existing = await session.scalar(
                select(DashboardStorageSnapshot).where(
                    DashboardStorageSnapshot.snapshot_date == current_time.date()
                )
            )
            if existing is None:
                session.add(
                    DashboardStorageSnapshot(
                        snapshot_date=current_time.date(),
                        source_path=storage.source_path,
                        total_bytes=storage.total_bytes,
                        used_bytes=storage.used_bytes,
                        free_bytes=storage.free_bytes,
                        used_percent=storage.used_percent,
                    )
                )
                return

            existing.source_path = storage.source_path
            existing.total_bytes = storage.total_bytes
            existing.used_bytes = storage.used_bytes
            existing.free_bytes = storage.free_bytes
            existing.used_percent = storage.used_percent

        await self._run_best_effort_cache_write(
            cache_key="dashboard_storage_snapshots",
            writer=_write,
        )

    async def should_refresh_rollups(
        self,
        snapshot: DashboardSnapshot,
        current_time: datetime,
    ) -> bool:
        """Return whether current-hour rollups are absent or stale."""
        bucket_start = _hour_bucket_start(current_time)
        try:
            existing_rows = (
                await self._session.execute(
                    select(DashboardMetricRollup.metric_key, DashboardMetricRollup.value).where(
                        DashboardMetricRollup.bucket_start == bucket_start
                    )
                )
            ).all()
        except (OperationalError, DatabaseError) as exc:
            await self._rollback_after_cache_error()
            self._log_cache_error(cache_key="dashboard_metric_rollups", exc=exc)
            return False
        if len(existing_rows) < len(_ROLLUP_KEYS):
            return True

        existing = {str(metric_key): float(value) for metric_key, value in existing_rows}
        if int(existing.get("review_debt_total", -1)) != snapshot.review_debt.total:
            return True
        if int(existing.get("release_risk_count", -1)) != snapshot.release_risk.next_72h_count:
            return True
        return int(existing.get("health_problem_count", -1)) != snapshot.health.problem_count

    async def persist_rollups(self, snapshot: DashboardSnapshot, current_time: datetime) -> None:
        """Persist current-hour dashboard metric rollups."""
        bucket_start = _hour_bucket_start(current_time)
        bucket_end = bucket_start + timedelta(hours=1)
        rollups = dashboard_rollup_payload(snapshot)

        async def _write(session: AsyncSession) -> None:
            for metric_key, (value, context) in rollups.items():
                existing = await session.scalar(
                    select(DashboardMetricRollup).where(
                        DashboardMetricRollup.metric_key == metric_key,
                        DashboardMetricRollup.bucket_start == bucket_start,
                    )
                )
                if existing is None:
                    session.add(
                        DashboardMetricRollup(
                            metric_key=metric_key,
                            bucket_start=bucket_start,
                            bucket_end=bucket_end,
                            value=value,
                            context_json=context,
                        )
                    )
                    continue
                existing.bucket_end = bucket_end
                existing.value = value
                existing.context_json = context

        await self._run_best_effort_cache_write(
            cache_key="dashboard_metric_rollups",
            writer=_write,
        )
