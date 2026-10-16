"""DB-backed scheduler task-stat persistence helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, func, make_url, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import ArgumentError, DatabaseError, OperationalError

from pullbox.core.scheduler_error_helpers import (
    is_locked_error,
    is_missing_task_stats_table_error,
    is_unusable_persist_error,
)
from pullbox.core.scheduler_stats import (
    TaskStats,
    load_legacy_task_stats_sidecar,
    merge_stats_into,
    parse_stat_timestamp,
    stats_from_row,
    stats_persisted_timestamp,
)
from pullbox.models.scheduler_task_stat import ScheduledTaskStat

logger = structlog.get_logger(__name__)

UpsertTaskStatRow = Callable[[Any, str, TaskStats], Awaitable[None]]
MarkStatsPersisted = Callable[[str, TaskStats], None]
LogMissingPersistedTable = Callable[..., None]
DisableTaskStatsPersistence = Callable[..., None]
IsHotTask = Callable[[str], bool]
Sleep = Callable[[float], Awaitable[None]]
LogPersistSkippedLocked = Callable[[str], None]
LogPersistFailed = Callable[[str], None]


@dataclass(frozen=True)
class LoadedTaskStat:
    """Persisted task stats plus the timestamp used for coarse throttling state."""

    task_id: str
    stats: TaskStats
    persisted_at: float | None


async def load_persisted_task_stat_rows(
    session: Any,
    *,
    known_task_ids: set[str],
) -> list[LoadedTaskStat]:
    """Load persisted task stats and prune rows for tasks no longer known to Pullbox."""
    result = await session.execute(select(ScheduledTaskStat))
    stale_task_ids: list[str] = []
    loaded: list[LoadedTaskStat] = []
    for row in result.scalars().all():
        if known_task_ids and row.task_id not in known_task_ids:
            stale_task_ids.append(row.task_id)
            continue
        stats = stats_from_row(row)
        loaded.append(
            LoadedTaskStat(
                task_id=row.task_id,
                stats=stats,
                persisted_at=stats_persisted_timestamp(stats),
            )
        )
    if stale_task_ids:
        await session.execute(
            delete(ScheduledTaskStat).where(ScheduledTaskStat.task_id.in_(stale_task_ids))
        )
        await session.commit()
    return loaded


async def persist_task_stat_with_retries(
    *,
    session_factory: Any,
    persist_lock: asyncio.Lock | None,
    task_id: str,
    stats: TaskStats,
    upsert_task_stat_row: UpsertTaskStatRow,
    mark_stats_persisted: MarkStatsPersisted,
    log_missing_persisted_table: LogMissingPersistedTable,
    disable_task_stats_persistence: DisableTaskStatsPersistence,
    is_hot_task: IsHotTask,
    log_persist_skipped_locked: LogPersistSkippedLocked | None = None,
    log_persist_failed: LogPersistFailed | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Persist task stats through an async session with SQLite lock handling."""
    lock = persist_lock or _NullAsyncLock()
    async with lock:
        for attempt in range(8):
            async with session_factory() as session:
                try:
                    await upsert_task_stat_row(session, task_id, stats)
                    await session.commit()
                    mark_stats_persisted(task_id, stats)
                    return
                except OperationalError as exc:
                    await session.rollback()
                    if is_missing_task_stats_table_error(exc):
                        log_missing_persisted_table(
                            task_id=task_id,
                            table_name="scheduled_task_stats",
                            phase="stats",
                        )
                        return
                    if is_locked_error(exc) and attempt < 7:
                        await sleep(0.25 * (attempt + 1))
                        continue
                    if is_locked_error(exc) and is_hot_task(task_id):
                        mark_stats_persisted(task_id, stats)
                        if log_persist_skipped_locked is not None:
                            log_persist_skipped_locked(task_id)
                        return
                    if is_unusable_persist_error(exc):
                        disable_task_stats_persistence(task_id=task_id, exc=exc)
                        return
                    if log_persist_failed is not None:
                        log_persist_failed(task_id)
                    return
                except DatabaseError as exc:
                    await session.rollback()
                    if is_unusable_persist_error(exc):
                        disable_task_stats_persistence(task_id=task_id, exc=exc)
                        return
                    if log_persist_failed is not None:
                        log_persist_failed(task_id)
                    return
                except Exception as exc:
                    await session.rollback()
                    if is_unusable_persist_error(exc):
                        disable_task_stats_persistence(task_id=task_id, exc=exc)
                        return
                    if log_persist_failed is not None:
                        log_persist_failed(task_id)
                    return


async def upsert_task_stat_row(session: Any, task_id: str, stats: TaskStats) -> None:
    """Upsert a scheduled task stats row into the main DB."""
    execution_dt = parse_stat_timestamp(stats.last_execution)
    last_missed_dt = parse_stat_timestamp(stats.last_missed_at)
    last_overlap_dt = parse_stat_timestamp(stats.last_overlap_at)
    last_exclusive_block_dt = parse_stat_timestamp(stats.last_exclusive_block_at)
    stmt = sqlite_insert(ScheduledTaskStat).values(
        task_id=task_id,
        last_execution=execution_dt,
        last_duration_seconds=stats.last_duration_seconds,
        last_status=stats.last_status,
        last_missed_at=last_missed_dt,
        missed_count=stats.missed_count,
        last_overlap_at=last_overlap_dt,
        overlap_count=stats.overlap_count,
        last_exclusive_block_at=last_exclusive_block_dt,
        exclusive_block_count=stats.exclusive_block_count,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ScheduledTaskStat.task_id],
        set_={
            "last_execution": execution_dt,
            "last_duration_seconds": stats.last_duration_seconds,
            "last_status": stats.last_status,
            "last_missed_at": last_missed_dt,
            "missed_count": stats.missed_count,
            "last_overlap_at": last_overlap_dt,
            "overlap_count": stats.overlap_count,
            "last_exclusive_block_at": last_exclusive_block_dt,
            "exclusive_block_count": stats.exclusive_block_count,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


def resolve_runtime_db_url(default_db_url: str, session_factory: Any) -> str:
    """Return the active DB URL, preferring the bound session engine when present."""
    bind = getattr(session_factory, "kw", {}).get("bind")
    if bind is not None and getattr(bind, "url", None) is not None:
        return str(bind.url)
    return default_db_url


def resolve_legacy_task_stats_sidecar_path(db_url: str) -> Path | None:
    """Return the retired JSON sidecar path for one-time import."""
    try:
        url = make_url(db_url)
    except ArgumentError:
        return None
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    uri = url.query.get("uri")
    if (
        url.database.startswith("file:")
        and isinstance(uri, str)
        and uri.strip().lower() in {"true", "1", "yes", "on", "y", "t"}
        and (url.database == "file::memory:" or url.query.get("mode") == "memory")
    ):
        return None
    db_path = Path(url.database)
    return db_path.with_name("scheduled_task_stats.json")


async def import_legacy_task_stats_sidecar(
    session: Any,
    sidecar_path: Path,
    *,
    known_task_ids: set[str],
    upsert_task_stat_row: UpsertTaskStatRow,
    log_missing_persisted_table: LogMissingPersistedTable,
) -> None:
    """Import a legacy JSON sidecar into the DB once, then delete it."""
    imported = await asyncio.to_thread(load_legacy_task_stats_sidecar, sidecar_path)
    if not imported:
        return
    if known_task_ids:
        imported = {
            task_id: stats for task_id, stats in imported.items() if task_id in known_task_ids
        }
    if not imported:
        sidecar_path.unlink(missing_ok=True)
        return

    try:
        result = await session.execute(
            select(ScheduledTaskStat).where(ScheduledTaskStat.task_id.in_(imported.keys()))
        )
        existing_rows = {row.task_id: row for row in result.scalars().all()}
        for task_id, imported_stats in imported.items():
            existing_stats = (
                stats_from_row(existing_rows[task_id]) if task_id in existing_rows else TaskStats()
            )
            merge_stats_into(existing_stats, imported_stats)
            await upsert_task_stat_row(session, task_id, existing_stats)
        await session.commit()
    except OperationalError as exc:
        await session.rollback()
        if is_missing_task_stats_table_error(exc):
            log_missing_persisted_table(
                task_id="scheduler",
                table_name="scheduled_task_stats",
                phase="stats_import",
            )
            return
        logger.warning(
            "scheduler_task_stats_sidecar_import_failed",
            path=str(sidecar_path),
            exc_info=True,
        )
        return
    except Exception:
        await session.rollback()
        logger.warning(
            "scheduler_task_stats_sidecar_import_failed",
            path=str(sidecar_path),
            exc_info=True,
        )
        return

    try:
        await asyncio.to_thread(sidecar_path.unlink)
    except OSError:
        logger.warning(
            "scheduler_task_stats_sidecar_cleanup_failed",
            path=str(sidecar_path),
            exc_info=True,
        )


class _NullAsyncLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None
