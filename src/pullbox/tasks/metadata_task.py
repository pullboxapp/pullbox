"""Metadata background tasks — syncs new issues and refreshes stale metadata.

Two scheduled tasks:
- ``sync_new_issues`` (daily cron) — fetches issue lists for
  ComicVine-backed series, creates new Issue records, and refreshes stale
  series metadata changes (status, description, publisher).  When new issues
  are set to WANTED by monitoring criteria, a one-shot search is scheduled.
- ``refresh_metadata`` (cron, default 03:15) — re-fetches series metadata
  when it exceeds the configured staleness threshold.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, or_, select

from pullbox.config import PullboxSettings, get_settings
from pullbox.core.exceptions import ProviderError
from pullbox.core.log_deduper import log_deduped_warning
from pullbox.core.sqlite_lock import is_sqlite_locked_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.comicvine_key import get_comicvine_api_key
from pullbox.core.scheduler import TaskExecutionResult, get_scheduler
from pullbox.database import get_session_factory
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus
from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider
from pullbox.services.metadata_service import MetadataService
from pullbox.tasks.metadata_sweep_state import load_sweep, save_sweep, schedule_sweep, start_sweep

logger = structlog.get_logger(__name__)

_RECENT_ISSUE_SYNC_LIMIT = 100
_METADATA_BATCH_SIZE = 25
_METADATA_BATCH_SECONDS = 120.0
_METADATA_SERIES_SECONDS = 900.0
_STANDARD_ISSUE_CHECK_INTERVAL = timedelta(hours=24)
_ENDED_MONITORED_ISSUE_CHECK_INTERVAL = timedelta(days=14)
_ENDED_UNMONITORED_ISSUE_CHECK_INTERVAL = timedelta(days=30)


async def _create_metadata_service(
    api_key: str,
    settings: PullboxSettings,
    session: AsyncSession,
) -> MetadataService:
    """Build a ComicVineProvider + MetadataService from an API key and settings."""
    from pullbox.services.cover_resolver import resolve_covers_dir

    provider = ComicVineProvider(
        api_key=api_key,
        rate_limit=settings.comicvine_rate_limit,
    )
    covers_dir = await resolve_covers_dir(session)
    return MetadataService(
        provider,
        covers_dir=covers_dir,
        refresh_days=settings.metadata_refresh_days,
    )


@dataclass
class _SeriesSnapshot:
    """Snapshot of series fields for change detection."""

    status: str
    description: str | None
    publisher_id: int | None


def _take_snapshot(series: Series) -> _SeriesSnapshot:
    """Capture current series metadata for later comparison."""
    return _SeriesSnapshot(
        status=str(series.status),
        description=series.description,
        publisher_id=series.publisher_id,
    )


def _detect_changes(
    series: Series,
    before: _SeriesSnapshot,
    log: structlog.stdlib.BoundLogger,
) -> tuple[bool, bool]:
    """Compare series against its snapshot. Returns (status_changed, metadata_changed)."""
    status_changed = False
    metadata_changed = False

    current_status = str(series.status)
    if current_status != before.status:
        status_changed = True
        log.info(
            "series_status_changed",
            old_status=before.status,
            new_status=current_status,
        )

    if series.description != before.description:
        metadata_changed = True
        log.debug("series_description_updated")

    if series.publisher_id != before.publisher_id:
        metadata_changed = True
        log.debug("series_publisher_updated")

    return status_changed, metadata_changed


def _metadata_refresh_due(series: Series, refresh_days: int) -> bool:
    """Return True when series-level metadata is missing or stale."""
    refreshed = series.metadata_last_refreshed
    if refreshed is None:
        return True
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - refreshed).days >= refresh_days


def _metadata_refresh_days(settings: PullboxSettings) -> int:
    """Read metadata refresh days defensively for tests and runtime overrides."""
    try:
        return int(settings.metadata_refresh_days)
    except (TypeError, ValueError):
        return 30


def _can_bootstrap_complete_catalog(series: Series, local_issue_count: int) -> bool:
    """Return True when a legacy complete catalog can safely start with recent sync."""
    provider_issue_count = int(series.issue_count or 0)
    return (
        series.issue_catalog_state == IssueCatalogState.COMPLETE
        and series.issue_catalog_last_synced_at is None
        and provider_issue_count > 0
        and local_issue_count >= provider_issue_count
    )


def _issue_catalog_full_sync_due(
    series: Series,
    refresh_days: int,
    *,
    local_issue_count: int = 0,
) -> bool:
    """Return True when scheduled issue sync should fetch the full issue list."""
    if refresh_days <= 0:
        return True
    if series.issue_catalog_state != IssueCatalogState.COMPLETE:
        return True
    synced = series.issue_catalog_last_synced_at
    if synced is None:
        return not _can_bootstrap_complete_catalog(series, local_issue_count)
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=UTC)
    return (datetime.now(UTC) - synced).days >= refresh_days


def _issue_check_interval_for_series(series: Series) -> timedelta:
    """Return the normal issue-check cadence for a complete catalog."""
    if series.status == SeriesStatus.ENDED:
        if series.monitored:
            return _ENDED_MONITORED_ISSUE_CHECK_INTERVAL
        return _ENDED_UNMONITORED_ISSUE_CHECK_INTERVAL
    return _STANDARD_ISSUE_CHECK_INTERVAL


def _issue_catalog_check_due(series: Series, *, now: datetime | None = None) -> bool:
    """Return True when a series should spend a ComicVine request on issue checks."""
    if series.issue_catalog_state != IssueCatalogState.COMPLETE:
        return True

    checked = series.issue_catalog_last_checked_at
    if checked is None:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)

    current_time = now or datetime.now(UTC)
    interval = _issue_check_interval_for_series(series)
    return current_time - checked >= interval


async def _sync_issue_catalog_for_series(
    metadata_svc: MetadataService,
    session: AsyncSession,
    series: Series,
    *,
    full_refresh_days: int,
    local_issue_count: int = 0,
) -> tuple[list[Issue], str]:
    """Sync one series' issue catalog using full or recent ComicVine issue fetches."""
    bootstrap_complete_catalog = _can_bootstrap_complete_catalog(series, local_issue_count)
    checked_at = datetime.now(UTC)
    if _issue_catalog_full_sync_due(
        series,
        full_refresh_days,
        local_issue_count=local_issue_count,
    ):
        created = await metadata_svc.fetch_issues_for_series(session, series.id)
        mode = "full"
        series.issue_catalog_last_synced_at = checked_at
        series.issue_catalog_last_checked_at = checked_at
    else:
        created = await metadata_svc.fetch_recent_issues_for_series(
            session,
            series.id,
            limit=_RECENT_ISSUE_SYNC_LIMIT,
        )
        mode = "recent"
        series.issue_catalog_last_checked_at = checked_at
        if bootstrap_complete_catalog:
            series.issue_catalog_last_synced_at = checked_at

    series.issue_catalog_state = IssueCatalogState.COMPLETE
    series.issue_catalog_error = None
    return created, mode


async def _sync_one_series(
    metadata_svc: MetadataService,
    session: AsyncSession,
    series: Series,
    *,
    refresh_days: int,
    local_issue_count: int,
) -> tuple[list[Issue], str, bool, bool]:
    before = _take_snapshot(series)
    log = logger.bind(series_id=series.id, title=series.title)
    status_changed = metadata_changed = False
    if series.comicvine_id and _metadata_refresh_due(series, refresh_days):
        await metadata_svc.fetch_series(session, series.comicvine_id, download_cover=False)
        # Provider waits must never retain the publisher/series writer lock.
        await session.commit()
        await session.refresh(series)
        status_changed, metadata_changed = _detect_changes(series, before, log)
        if series.cover_url:
            await metadata_svc.download_series_cover(series, series.cover_url)
            await session.commit()

    if not _issue_catalog_check_due(series):
        return [], "skipped", status_changed, metadata_changed
    created, mode = await _sync_issue_catalog_for_series(
        metadata_svc,
        session,
        series,
        full_refresh_days=refresh_days,
        local_issue_count=local_issue_count,
    )
    return created, mode, status_changed, metadata_changed


def _provider_pause_seconds(exc: Exception) -> float | None:
    if is_sqlite_locked_error(exc):
        return 60
    if isinstance(exc, TimeoutError):
        return 300
    details = exc.details or {} if isinstance(exc, ProviderError) else {}
    status = exc.status_code if isinstance(exc, ComicVineError) else details.get("status_code")
    retryable = exc.retryable if isinstance(exc, ComicVineError) else details.get("retryable")
    if status in {100, 107, 401, 403, 420, 429}:
        retry = (
            exc.retry_after_seconds
            if isinstance(exc, ComicVineError)
            else details.get("retry_after_seconds")
        )
        return float(retry) if retry else 3600
    return 300 if retryable else None


async def _run_metadata_sweep(task_id: str) -> TaskExecutionResult:
    settings = get_settings()
    factory = get_session_factory()
    series_to_search: list[int] = []
    started = time.monotonic()
    async with factory() as session:
        api_key = await get_comicvine_api_key(session)
        if not api_key:
            state = await load_sweep(session, task_id)
            if state.active:
                state.active = False
                state.retry_at = 0
                await save_sweep(session, task_id, state)
                await session.commit()
            schedule_sweep(task_id, state)
            log_deduped_warning(
                logger,
                f"{task_id}_missing_comicvine_key",
                key=f"{task_id}_missing_comicvine_key",
                action_required="Configure a ComicVine API key to enable metadata sync.",
            )
            return TaskExecutionResult(status="completed")

        state = await start_sweep(session, task_id)
        schedule_sweep(task_id, state)
        if state.retry_at > datetime.now(UTC).timestamp():
            schedule_sweep(task_id, state)
            return TaskExecutionResult(status="waiting")

        refresh_days = _metadata_refresh_days(settings)
        predicates = [
            Series.comicvine_id.isnot(None),
            Series.id > state.cursor,
            Series.id <= state.upper_bound,
        ]
        if task_id == "refresh_metadata":
            predicates.extend(
                [
                    Series.monitored.is_(True),
                    or_(
                        Series.metadata_last_refreshed.is_(None),
                        Series.metadata_last_refreshed
                        < datetime.now(UTC) - timedelta(days=refresh_days),
                    ),
                ]
            )
        ids = list(
            (
                await session.scalars(
                    select(Series.id)
                    .where(*predicates)
                    .order_by(Series.id)
                    .limit(_METADATA_BATCH_SIZE + 1)
                )
            ).all()
        )
        if not ids:
            state.active = False
            state.retry_at = 0
            await save_sweep(session, task_id, state)
            await session.commit()
            schedule_sweep(task_id, state)
            return TaskExecutionResult(status="completed")

        counts = {
            int(series_id): int(count)
            for series_id, count in (
                await session.execute(
                    select(Issue.series_id, func.count(Issue.id))
                    .where(Issue.series_id.in_(ids[:_METADATA_BATCH_SIZE]))
                    .group_by(Issue.series_id)
                )
            ).all()
        }
        metadata_svc = await _create_metadata_service(api_key, settings, session)
        await session.commit()
        processed = failed = new_issues = 0
        paused = False
        try:
            for series_id in ids[:_METADATA_BATCH_SIZE]:
                if processed and time.monotonic() - started >= _METADATA_BATCH_SECONDS:
                    break
                series = await session.get(Series, series_id)
                if series is None:
                    state.cursor = series_id
                    await save_sweep(session, task_id, state)
                    await session.commit()
                    processed += 1
                    continue
                try:
                    previous_cursor = state.cursor
                    search_after_commit = False
                    async with asyncio.timeout(_METADATA_SERIES_SECONDS):
                        if task_id == "refresh_metadata":
                            await metadata_svc.refresh_series(
                                session,
                                series_id,
                                commit_before_provider_wait=True,
                            )
                        else:
                            created, _mode, _sc, _mc = await _sync_one_series(
                                metadata_svc,
                                session,
                                series,
                                refresh_days=refresh_days,
                                local_issue_count=counts.get(series_id, 0),
                            )
                            new_issues += len(created)
                            if created and series.monitored:
                                wanted = False
                                for issue in created:
                                    if issue.status == IssueStatus.SKIPPED:
                                        issue.status = IssueStatus.WANTED
                                        wanted = True
                                if wanted:
                                    search_after_commit = True
                    state.cursor = series_id
                    state.retry_at = 0
                    await save_sweep(session, task_id, state)
                    await session.commit()
                    if search_after_commit:
                        series_to_search.append(series_id)
                    processed += 1
                except Exception as exc:
                    await session.rollback()
                    state.cursor = previous_cursor
                    # Rollback expires ORM objects; use the stable ID, not their fields.
                    pause_seconds = _provider_pause_seconds(exc)
                    if pause_seconds is not None:
                        state.retry_at = (
                            datetime.now(UTC) + timedelta(seconds=pause_seconds)
                        ).timestamp()
                        await save_sweep(session, task_id, state)
                        await session.commit()
                        paused = True
                        logger.warning(
                            "metadata_sweep_paused",
                            task_id=task_id,
                            series_id=series_id,
                            retry_seconds=pause_seconds,
                            failure_type=type(exc).__name__,
                        )
                        break
                    failed += 1
                    processed += 1
                    state.cursor = series_id
                    await save_sweep(session, task_id, state)
                    await session.commit()
                    logger.exception(f"{task_id}_series_failed", series_id=series_id)

            state.active = paused or processed < len(ids)
            await save_sweep(session, task_id, state)
            await session.commit()
        finally:
            close = getattr(metadata_svc, "close", None)
            if close is not None:
                await close()

    if series_to_search:
        from pullbox.tasks.search_task import search_series_issues

        for sid in series_to_search:
            get_scheduler()._scheduler.add_job(
                search_series_issues,
                trigger="date",
                args=[sid],
                id=f"search_new_{sid}_{int(time.time())}",
                misfire_grace_time=300,
            )
    schedule_sweep(task_id, state)
    logger.info(
        f"{task_id}_batch_complete",
        series_checked=processed,
        new_issues=new_issues,
        failed=failed,
        cursor=state.cursor,
        upper_bound=state.upper_bound,
        waiting=state.active,
    )
    return TaskExecutionResult(status="waiting" if state.active else "completed")


async def sync_new_issues() -> TaskExecutionResult:
    """Resume a bounded all-series issue sweep without monopolizing the scheduler."""
    return await _run_metadata_sweep("sync_new_issues")


async def refresh_metadata() -> TaskExecutionResult:
    """Resume a bounded sweep of stale monitored-series metadata."""
    return await _run_metadata_sweep("refresh_metadata")
