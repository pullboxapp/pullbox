"""Direct coverage for scheduler task-stat persistence helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import URL, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.scheduler_stats import TaskStats
from pullbox.core.scheduler_stats_persistence import (
    load_persisted_task_stat_rows,
    persist_task_stat_with_retries,
    resolve_legacy_task_stats_sidecar_path,
    resolve_runtime_db_url,
    upsert_task_stat_row,
)
from pullbox.models.scheduler_task_stat import ScheduledTaskStat

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_upsert_task_stat_row_round_trips_all_event_fields(async_engine) -> None:
    """The extracted upsert helper preserves execution and scheduler incident fields."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    stats = TaskStats(
        last_execution=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC).isoformat(),
        last_duration_seconds=12.5,
        last_status="completed",
        last_missed_at=datetime(2026, 6, 1, 11, 30, 0, tzinfo=UTC).isoformat(),
        missed_count=2,
        last_overlap_at=datetime(2026, 6, 1, 11, 45, 0, tzinfo=UTC).isoformat(),
        overlap_count=3,
        last_exclusive_block_at=datetime(2026, 6, 1, 11, 50, 0, tzinfo=UTC).isoformat(),
        exclusive_block_count=4,
    )

    async with factory() as session:
        await upsert_task_stat_row(session, "sync_new_issues", stats)
        await session.commit()

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "sync_new_issues")

    assert row is not None
    assert row.last_execution == datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert row.last_duration_seconds == 12.5
    assert row.last_status == "completed"
    assert row.last_missed_at == datetime(2026, 6, 1, 11, 30, 0, tzinfo=UTC)
    assert row.missed_count == 2
    assert row.last_overlap_at == datetime(2026, 6, 1, 11, 45, 0, tzinfo=UTC)
    assert row.overlap_count == 3
    assert row.last_exclusive_block_at == datetime(2026, 6, 1, 11, 50, 0, tzinfo=UTC)
    assert row.exclusive_block_count == 4


@pytest.mark.asyncio
async def test_load_persisted_task_stat_rows_prunes_unknown_tasks(async_engine) -> None:
    """The extracted load helper returns known rows and deletes stale scheduler rows."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                ScheduledTaskStat(task_id="search_wanted", last_status="completed"),
                ScheduledTaskStat(task_id="retired_task", last_status="failed"),
            ]
        )
        await session.commit()

    async with factory() as session:
        loaded = await load_persisted_task_stat_rows(
            session,
            known_task_ids={"search_wanted"},
        )

    assert [row.task_id for row in loaded] == ["search_wanted"]
    assert loaded[0].stats.last_status == "completed"

    async with factory() as session:
        remaining = await session.scalars(select(ScheduledTaskStat.task_id))

    assert set(remaining.all()) == {"search_wanted"}


@pytest.mark.asyncio
async def test_persist_task_stat_with_retries_recovers_from_transient_lock(async_engine) -> None:
    """Transient SQLite locks should retry and mark stats persisted after success."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    upsert = AsyncMock(
        side_effect=[
            OperationalError("INSERT", {}, Exception("database is locked")),
            None,
        ]
    )
    mark_persisted = MagicMock()
    sleep = AsyncMock()

    await persist_task_stat_with_retries(
        session_factory=factory,
        persist_lock=None,
        task_id="monitor_downloads",
        stats=TaskStats(last_status="completed"),
        upsert_task_stat_row=upsert,
        mark_stats_persisted=mark_persisted,
        log_missing_persisted_table=MagicMock(),
        disable_task_stats_persistence=MagicMock(),
        is_hot_task=MagicMock(return_value=False),
        sleep=sleep,
    )

    assert upsert.await_count == 2
    sleep.assert_awaited_once()
    mark_persisted.assert_called_once()


def test_resolve_runtime_db_url_prefers_session_factory_bind(async_engine) -> None:
    """The helper keeps the bound engine URL precedence used by the scheduler."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    assert resolve_runtime_db_url("sqlite+aiosqlite:////config/pullbox.db", factory) == str(
        async_engine.url
    )


def test_resolve_legacy_task_stats_sidecar_path_uses_sqlite_db_neighbor() -> None:
    """Legacy sidecar imports should look next to the SQLite database file."""
    sidecar = resolve_legacy_task_stats_sidecar_path(
        "sqlite+aiosqlite:////config/pullbox/pullbox.db?cache=shared"
    )

    assert sidecar is not None
    assert str(sidecar) == "/config/pullbox/scheduled_task_stats.json"
    assert resolve_legacy_task_stats_sidecar_path("sqlite+aiosqlite:///:memory:") is None
    assert resolve_legacy_task_stats_sidecar_path("postgresql://example") is None


@pytest.mark.parametrize(
    ("driver", "database", "expected_directory"),
    [
        ("sqlite+aiosqlite", ":memory:", None),
        ("sqlite+aiosqlite", None, None),
        ("sqlite+aiosqlite", "", None),
        ("sqlite+aiosqlite", "/tmp/ordinary/pullbox.db", "/tmp/ordinary"),
        ("sqlite+aiosqlite", "/tmp/library:one/pullbox.db", "/tmp/library:one"),
        ("sqlite+aiosqlite", "/tmp/library:memory:/pullbox.db", "/tmp/library:memory:"),
        ("sqlite+aiosqlite", "/tmp/library?one/pullbox.db", "/tmp/library?one"),
        ("sqlite+aiosqlite", "/tmp/library%3Aone/pullbox.db", "/tmp/library%3Aone"),
        ("postgresql+asyncpg", "sqlite:memory:", None),
    ],
)
async def test_legacy_sidecar_uses_structured_runtime_database(
    driver: str, database: str | None, expected_directory: str | None
) -> None:
    """A rendered engine URL must not become an encoded or imaginary file path."""
    url = URL.create(driver, database=database)
    if driver == "postgresql+asyncpg":
        # URL resolution does not require a connection or the optional database driver.
        engine = cast("AsyncEngine", SimpleNamespace(url=url))
    else:
        engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        runtime_url = resolve_runtime_db_url("sqlite+aiosqlite:////unused/default.db", factory)
        expected = (
            Path(expected_directory) / "scheduled_task_stats.json"
            if expected_directory is not None
            else None
        )
        assert resolve_legacy_task_stats_sidecar_path(runtime_url) == expected
    finally:
        if driver == "sqlite+aiosqlite":
            await engine.dispose()


@pytest.mark.parametrize("db_url", ["", "not-a-sqlite-url", "https://sqlite.example/database"])
def test_legacy_sidecar_rejects_non_database_urls(db_url: str) -> None:
    assert resolve_legacy_task_stats_sidecar_path(db_url) is None


@pytest.mark.parametrize(
    ("database", "query", "is_memory"),
    [
        ("file::memory:", {"uri": "true"}, True),
        ("file:sidecar", {"uri": "true", "mode": "memory"}, True),
        ("file:sidecar", {"uri": " YES ", "mode": "memory"}, True),
        ("file::memory:", {"uri": "t"}, True),
        ("file:sidecar", {"uri": "false", "mode": "memory"}, False),
        ("file:sidecar", {"uri": "true", "mode": "rwc"}, False),
        ("file:sidecar", {"uri": "true", "cache": "mode=memory"}, False),
        ("file:sidecar", {"mode": "memory"}, False),
    ],
)
def test_legacy_sidecar_classifies_uri_memory_without_query_substrings(
    database: str, query: dict[str, str], is_memory: bool
) -> None:
    url = URL.create("sqlite+aiosqlite", database=database, query=query)
    expected = None if is_memory else Path("scheduled_task_stats.json")
    assert resolve_legacy_task_stats_sidecar_path(str(url)) == expected
