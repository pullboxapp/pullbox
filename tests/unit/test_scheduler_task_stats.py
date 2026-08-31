"""Scheduler task-stat persistence, throttling, and manual-run coverage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullbox.tasks  # noqa: F401
from pullbox.core.scheduler import (
    PullboxScheduler,
    TaskExecutionResult,
    TaskStats,
    _task_registry,
    get_current_task_run_id,
    scheduled_task,
)
from pullbox.models import Base
from pullbox.models.scheduler_task_stat import ScheduledTaskStat

_BASE_TASK_REGISTRY = list(_task_registry)


def _iso_from_epoch(epoch_seconds: float) -> str:
    """Convert an epoch timestamp to an ISO-8601 UTC string."""
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


@pytest.fixture(autouse=True)
def _reset_scheduler_registry() -> None:
    """Keep the decorator registry stable across scheduler tests."""
    _task_registry.clear()
    _task_registry.extend(_BASE_TASK_REGISTRY)
    yield
    _task_registry.clear()
    _task_registry.extend(_BASE_TASK_REGISTRY)


@pytest.mark.asyncio
async def test_manual_run_updates_scheduler_task_stats(
    async_engine,
    monkeypatch,
) -> None:
    """A manual run should update last execution and status for the scheduled task row."""
    _task_registry.clear()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    @scheduled_task(task_id="test_manual_refresh", trigger="interval", display_name="Test", hours=1)
    async def _dummy_task() -> None:
        return None

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    try:
        assert scheduler.run_task_now("test_manual_refresh") == "queued"
        await asyncio.sleep(0.5)
        tasks = scheduler.get_scheduled_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task["last_execution"] is not None
        assert task["last_status"] == "completed"
        async with factory() as session:
            row = await session.get(ScheduledTaskStat, "test_manual_refresh")
            assert row is not None
            assert row.last_status == "completed"
            assert row.last_execution is not None
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_manual_run_duplicate_is_rejected_while_task_is_running(monkeypatch) -> None:
    """A duplicate manual trigger should be rejected instead of recording overlap."""
    _task_registry.clear()
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )

    @scheduled_task(
        task_id="test_overlap_task",
        trigger="interval",
        display_name="Test Overlap Task",
        hours=1,
    )
    async def _long_task() -> None:
        await asyncio.sleep(0.25)

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        assert scheduler.run_task_now("test_overlap_task") == "queued"
        await asyncio.sleep(0.05)
        assert scheduler.run_task_now("test_overlap_task") == "already_running"
        await asyncio.sleep(0.35)

        task = scheduler.get_scheduled_tasks()[0]
        assert task["last_status"] == "completed"
        assert task["overlap_count"] == 0
        assert task["last_overlap_at"] is None
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_cancelled_run_updates_scheduler_task_state(monkeypatch) -> None:
    """Cancelled runs should persist the most recent interrupted state."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]

    async def _long_task() -> None:
        await asyncio.sleep(1)

    wrapped = scheduler._wrap_task(_long_task, "test_cancelled_task")
    task = asyncio.create_task(wrapped())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    stats = scheduler._task_stats["test_cancelled_task"]
    assert stats.last_status == "cancelled"
    assert stats.last_execution is not None
    assert stats.last_duration_seconds is not None
    assert stats.running_since is None


@pytest.mark.asyncio
async def test_task_can_report_waiting_without_being_marked_completed(monkeypatch) -> None:
    """A bounded batch may finish while its durable logical task remains active."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        AsyncMock(return_value=False),
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]

    async def _waiting_task() -> TaskExecutionResult:
        return TaskExecutionResult(status="waiting")

    await scheduler._wrap_task(_waiting_task, "test_waiting_task")()

    stats = scheduler._task_stats["test_waiting_task"]
    assert stats.last_status == "waiting"
    scheduler._persist_task_stat.assert_awaited_once()
    assert scheduler._persist_task_stat.await_args.kwargs["reason"] == "waiting"


def test_scheduler_continuation_is_internal_and_restartable() -> None:
    """Continuation jobs share task stats but never add a duplicate Tasks row."""
    _task_registry.clear()

    @scheduled_task(
        task_id="test_sweep",
        trigger="interval",
        display_name="Test Sweep",
        hours=6,
    )
    async def _sweep() -> None:
        return None

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    run_at = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)

    assert scheduler.schedule_task_continuation("test_sweep", run_at=run_at) is True
    assert scheduler._scheduler.get_job("test_sweep__continuation") is not None
    assert [task["task_id"] for task in scheduler.get_scheduled_tasks()] == ["test_sweep"]

    scheduler.clear_task_continuation("test_sweep")
    assert scheduler._scheduler.get_job("test_sweep__continuation") is None


@pytest.mark.asyncio
async def test_scheduled_run_defers_while_import_is_protected(monkeypatch) -> None:
    """Scheduled work should not compete with active or stalled imports."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        AsyncMock(return_value=True),
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    called = False

    async def _task() -> None:
        nonlocal called
        called = True

    wrapped = scheduler._wrap_task(_task, "test_deferred_import_task")
    await wrapped()

    assert called is False
    stats = scheduler._task_stats["test_deferred_import_task"]
    assert stats.last_status == "deferred"
    assert stats.last_execution is not None
    assert stats.last_duration_seconds == 0
    assert stats.running_since is None
    scheduler._persist_task_stat.assert_awaited_once()
    assert scheduler._running_counts == {}


@pytest.mark.asyncio
async def test_manual_run_defers_while_import_is_protected(monkeypatch) -> None:
    """Queued manual task work should also defer once an import is protected."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )
    protection_check = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        protection_check,
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    called = False

    async def _task() -> None:
        nonlocal called
        called = True

    wrapped = scheduler._wrap_task(_task, "test_manual_import_task", trigger_type="manual")
    await wrapped()

    assert called is False
    protection_check.assert_awaited_once()
    stats = scheduler._task_stats["test_manual_import_task"]
    assert stats.last_status == "deferred"
    assert stats.last_execution is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type", ["scheduled", "manual"])
async def test_story_arc_sync_runs_through_import_protection(
    monkeypatch,
    trigger_type: str,
) -> None:
    """The import-owned placement drain must be the narrow scheduler exception."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )
    protection_check = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        protection_check,
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    called = False

    async def _story_arc_sync() -> None:
        nonlocal called
        called = True

    wrapped = scheduler._wrap_task(
        _story_arc_sync,
        "sync_story_arc_placements",
        trigger_type=trigger_type,
    )
    await wrapped()

    assert called is True
    protection_check.assert_not_awaited()
    assert scheduler._task_stats["sync_story_arc_placements"].last_status == "completed"


@pytest.mark.asyncio
async def test_running_task_is_exposed_in_scheduler_rows() -> None:
    """A currently executing task should surface as running in scheduled task rows."""
    _task_registry.clear()

    @scheduled_task(
        task_id="test_long_running_task",
        trigger="interval",
        display_name="Test Long Task",
        hours=1,
    )
    async def _long_task() -> None:
        await asyncio.sleep(0.4)

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        assert scheduler.run_task_now("test_long_running_task") == "queued"
        await asyncio.sleep(0.05)
        task = scheduler.get_scheduled_tasks()[0]
        assert task["is_running"] is True
        assert task["running_since"] is not None
        await asyncio.sleep(0.5)
        task = scheduler.get_scheduled_tasks()[0]
        assert task["is_running"] is False
        assert task["last_status"] == "completed"
        assert task["running_since"] is None
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_scheduler_run_binds_log_context() -> None:
    """Scheduled task runs should bind task/run metadata into contextvars."""
    _task_registry.clear()
    captured: dict[str, object] = {}

    @scheduled_task(
        task_id="test_context_task",
        trigger="interval",
        display_name="Context Task",
        hours=1,
    )
    async def _context_task() -> None:
        captured.update(structlog.contextvars.get_contextvars())
        captured["current_task_run_id"] = get_current_task_run_id()

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    try:
        assert scheduler.run_task_now("test_context_task") == "queued"
        await asyncio.sleep(0.1)
        assert captured["task_id"] == "test_context_task"
        assert captured["trigger_type"] == "manual"
        assert isinstance(captured.get("run_id"), str)
        assert len(str(captured["run_id"])) >= 8
        assert captured["task_run_id"] == captured["run_id"]
        assert captured["current_task_run_id"] == captured["run_id"]
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_manual_scheduler_run_keeps_trigger_request_id_without_leaking_request_id() -> None:
    """Manual task runs should expose the triggering request ID separately."""
    _task_registry.clear()
    first_context: dict[str, object] = {}
    second_context: dict[str, object] = {}
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    @scheduled_task(task_id="test_manual_request_first", trigger="interval", hours=1)
    async def _first_task() -> None:
        first_context.update(structlog.contextvars.get_contextvars())
        first_started.set()
        await release_first.wait()

    @scheduled_task(task_id="test_manual_request_second", trigger="interval", hours=1)
    async def _second_task() -> None:
        second_context.update(structlog.contextvars.get_contextvars())

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        structlog.contextvars.bind_contextvars(request_id="req-first")
        try:
            assert scheduler.run_task_now("test_manual_request_first") == "queued"
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        structlog.contextvars.bind_contextvars(request_id="req-second")
        try:
            assert scheduler.run_task_now("test_manual_request_second") == "queued"
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        release_first.set()
        await asyncio.sleep(0.2)

        assert first_context["trigger_request_id"] == "req-first"
        assert "request_id" not in first_context
        assert second_context["trigger_request_id"] == "req-second"
        assert "request_id" not in second_context
        assert first_context["task_run_id"] != second_context["task_run_id"]
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_manual_runs_queue_serially_and_surface_queue_position() -> None:
    """Different manual task requests should drain one at a time in FIFO order."""
    _task_registry.clear()
    max_concurrency = 0
    current_concurrency = 0
    execution_order: list[str] = []

    @scheduled_task(
        task_id="test_queue_first",
        trigger="interval",
        display_name="Queue First",
        hours=1,
    )
    async def _first_task() -> None:
        nonlocal current_concurrency, max_concurrency
        current_concurrency += 1
        max_concurrency = max(max_concurrency, current_concurrency)
        execution_order.append("first")
        try:
            await asyncio.sleep(0.2)
        finally:
            current_concurrency -= 1

    @scheduled_task(
        task_id="test_queue_second",
        trigger="interval",
        display_name="Queue Second",
        hours=1,
    )
    async def _second_task() -> None:
        nonlocal current_concurrency, max_concurrency
        current_concurrency += 1
        max_concurrency = max(max_concurrency, current_concurrency)
        execution_order.append("second")
        try:
            await asyncio.sleep(0.05)
        finally:
            current_concurrency -= 1

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        assert scheduler.run_task_now("test_queue_first") == "queued"
        assert scheduler.run_task_now("test_queue_second") == "queued"
        await asyncio.sleep(0.05)

        tasks = {task["task_id"]: task for task in scheduler.get_scheduled_tasks()}
        assert tasks["test_queue_first"]["is_running"] is True
        assert tasks["test_queue_second"]["is_queued"] is True
        assert tasks["test_queue_second"]["manual_queue_position"] == 1

        await asyncio.sleep(0.35)

        tasks = {task["task_id"]: task for task in scheduler.get_scheduled_tasks()}
        assert tasks["test_queue_first"]["last_status"] == "completed"
        assert tasks["test_queue_second"]["last_status"] == "completed"
        assert tasks["test_queue_second"]["is_queued"] is False
        assert tasks["test_queue_second"]["manual_queue_position"] is None
        assert execution_order == ["first", "second"]
        assert max_concurrency == 1
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_health_checks_stay_last_in_manual_queue() -> None:
    """Manual health refreshes should defer until the rest of the queue drains."""
    _task_registry.clear()
    execution_order: list[str] = []

    @scheduled_task(
        task_id="run_health_checks",
        trigger="interval",
        display_name="Check Health",
        hours=1,
        exclusive=True,
    )
    async def _health_task() -> None:
        execution_order.append("health")

    @scheduled_task(
        task_id="test_queue_before_health",
        trigger="interval",
        display_name="Before Health",
        hours=1,
    )
    async def _first_task() -> None:
        execution_order.append("first")
        await asyncio.sleep(0.1)

    @scheduled_task(
        task_id="test_queue_after_health",
        trigger="interval",
        display_name="After Health",
        hours=1,
    )
    async def _second_task() -> None:
        execution_order.append("second")

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        assert scheduler.run_task_now("test_queue_before_health") == "queued"
        assert scheduler.run_task_now("run_health_checks") == "queued"
        assert scheduler.run_task_now("test_queue_after_health") == "queued"

        await asyncio.sleep(0.02)

        tasks = {task["task_id"]: task for task in scheduler.get_scheduled_tasks()}
        assert tasks["test_queue_before_health"]["is_running"] is True
        assert tasks["test_queue_after_health"]["manual_queue_position"] == 1
        assert tasks["run_health_checks"]["manual_queue_position"] == 2

        await asyncio.sleep(0.25)

        tasks = {task["task_id"]: task for task in scheduler.get_scheduled_tasks()}
        assert tasks["run_health_checks"]["last_status"] == "completed"
        assert tasks["test_queue_after_health"]["last_status"] == "completed"
        assert execution_order == ["first", "second", "health"]
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_exclusive_task_blocks_other_scheduler_runs() -> None:
    """Exclusive tasks should prevent other background jobs from starting concurrently."""
    _task_registry.clear()
    execution_order: list[str] = []

    @scheduled_task(
        task_id="test_exclusive_task",
        trigger="interval",
        display_name="Exclusive Task",
        hours=1,
        exclusive=True,
    )
    async def _exclusive_task() -> None:
        execution_order.append("exclusive")
        await asyncio.sleep(0.2)

    @scheduled_task(
        task_id="test_regular_task",
        trigger="interval",
        display_name="Regular Task",
        hours=1,
    )
    async def _regular_task() -> None:
        execution_order.append("regular")

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]
    try:
        assert scheduler.run_task_now("test_exclusive_task") == "queued"
        await asyncio.sleep(0.05)

        regular_wrapped = scheduler._wrap_task(_regular_task, "test_regular_task")
        await regular_wrapped()

        await asyncio.sleep(0.25)

        tasks = {task["task_id"]: task for task in scheduler.get_scheduled_tasks()}
        assert execution_order == ["exclusive"]
        assert tasks["test_exclusive_task"]["last_status"] == "completed"
        assert tasks["test_regular_task"]["last_exclusive_block_at"] is not None
        assert tasks["test_regular_task"]["exclusive_block_count"] == 1
        assert tasks["test_regular_task"]["last_overlap_at"] is None
        assert tasks["test_regular_task"]["overlap_count"] == 0
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_load_persisted_stats_merges_newer_task_state(
    async_engine,
    monkeypatch,
) -> None:
    """Persisted task stats should hydrate the scheduler with the newest known state."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    persisted_time = datetime(2026, 4, 20, 12, 34, 56, tzinfo=UTC).isoformat()
    async with factory() as session:
        session.add(
            ScheduledTaskStat(
                task_id="refresh_metadata",
                last_execution=datetime.fromisoformat(persisted_time),
                last_duration_seconds=2.75,
                last_status="completed",
            )
        )
        await session.commit()

    scheduler = PullboxScheduler()
    await scheduler.load_persisted_stats()

    tasks = scheduler._task_stats
    assert "refresh_metadata" in tasks
    assert tasks["refresh_metadata"].last_execution == persisted_time
    assert tasks["refresh_metadata"].last_duration_seconds == 2.75
    assert tasks["refresh_metadata"].last_status == "completed"

    async with factory() as session:
        row = await session.scalar(
            select(ScheduledTaskStat).where(ScheduledTaskStat.task_id == "refresh_metadata")
        )
        assert row is not None
        assert row.last_status == "completed"


@pytest.mark.asyncio
async def test_persist_task_stat_round_trips_db_fields(
    async_engine,
    monkeypatch,
) -> None:
    """DB-backed task stats should preserve execution and event metadata."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    scheduler = PullboxScheduler()
    stats = TaskStats(
        last_execution=datetime(2026, 4, 21, 5, 0, 0, tzinfo=UTC).isoformat(),
        last_duration_seconds=12.34,
        last_status="completed",
        last_missed_at=datetime(2026, 4, 21, 4, 30, 0, tzinfo=UTC).isoformat(),
        missed_count=2,
        last_overlap_at=datetime(2026, 4, 21, 4, 45, 0, tzinfo=UTC).isoformat(),
        overlap_count=3,
        last_exclusive_block_at=datetime(2026, 4, 21, 4, 50, 0, tzinfo=UTC).isoformat(),
        exclusive_block_count=4,
    )

    await scheduler._persist_task_stat("sync_new_issues", stats)

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "sync_new_issues")
        assert row is not None
        assert row.last_status == "completed"
        assert row.last_duration_seconds == 12.34
        assert row.last_missed_at == datetime(2026, 4, 21, 4, 30, 0, tzinfo=UTC)
        assert row.missed_count == 2
        assert row.last_overlap_at == datetime(2026, 4, 21, 4, 45, 0, tzinfo=UTC)
        assert row.overlap_count == 3
        assert row.last_exclusive_block_at == datetime(2026, 4, 21, 4, 50, 0, tzinfo=UTC)
        assert row.exclusive_block_count == 4

    reloaded = PullboxScheduler()
    await reloaded.load_persisted_stats()
    task = reloaded._task_stats["sync_new_issues"]
    assert task.last_status == "completed"
    assert task.last_duration_seconds == 12.34
    assert task.last_missed_at == stats.last_missed_at
    assert task.missed_count == 2
    assert task.last_overlap_at == stats.last_overlap_at
    assert task.overlap_count == 3
    assert task.last_exclusive_block_at == stats.last_exclusive_block_at
    assert task.exclusive_block_count == 4


@pytest.mark.asyncio
async def test_scheduler_event_stats_are_exposed_in_task_rows(monkeypatch) -> None:
    """Missed and max-instance overlap events should surface through task rows."""
    _task_registry.clear()
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )

    @scheduled_task(
        task_id="test_eventful_task",
        trigger="interval",
        display_name="Test Eventful Task",
        hours=1,
    )
    async def _eventful_task() -> None:
        return None

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    monkeypatch.setattr(scheduler, "_schedule_stat_persist", lambda _task_id, **_kwargs: None)
    try:
        scheduler._on_job_missed(type("Event", (), {"job_id": "test_eventful_task"})())
        scheduler._on_job_max_instances(type("Event", (), {"job_id": "test_eventful_task"})())

        task = scheduler.get_scheduled_tasks()[0]
        assert task["missed_count"] == 1
        assert task["overlap_count"] == 1
        assert task["last_missed_at"] is not None
        assert task["last_overlap_at"] is not None
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


@pytest.mark.asyncio
async def test_load_persisted_stats_imports_legacy_sidecar_and_removes_it(
    tmp_path,
    monkeypatch,
) -> None:
    """Legacy sidecar task stats should merge into the DB once, then disappear."""
    db_file = tmp_path / "scheduler_import.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": db_url})(),
    )

    sidecar_path = db_file.with_name("scheduled_task_stats.json")
    sidecar_path.write_text(
        json.dumps(
            {
                "search_wanted": {
                    "last_execution": datetime(2026, 4, 21, 5, 0, 0, tzinfo=UTC).isoformat(),
                    "last_duration_seconds": 7.89,
                    "last_status": "completed",
                    "last_missed_at": datetime(2026, 4, 22, 5, 30, 0, tzinfo=UTC).isoformat(),
                    "missed_count": 3,
                    "last_overlap_at": datetime(2026, 4, 22, 5, 45, 0, tzinfo=UTC).isoformat(),
                    "overlap_count": 5,
                    "last_exclusive_block_at": datetime(
                        2026, 4, 22, 5, 50, 0, tzinfo=UTC
                    ).isoformat(),
                    "exclusive_block_count": 7,
                },
                "unknown_task": {
                    "last_execution": datetime(2026, 4, 20, 5, 0, 0, tzinfo=UTC).isoformat(),
                    "last_status": "completed",
                },
            }
        )
    )

    async with factory() as session:
        session.add(
            ScheduledTaskStat(
                task_id="search_wanted",
                last_execution=datetime(2026, 4, 22, 6, 0, 0, tzinfo=UTC),
                last_duration_seconds=9.5,
                last_status="failed",
                last_missed_at=datetime(2026, 4, 22, 4, 30, 0, tzinfo=UTC),
                missed_count=1,
                last_overlap_at=datetime(2026, 4, 22, 4, 45, 0, tzinfo=UTC),
                overlap_count=1,
                last_exclusive_block_at=datetime(2026, 4, 22, 4, 50, 0, tzinfo=UTC),
                exclusive_block_count=1,
            )
        )
        await session.commit()

    reloaded = PullboxScheduler()
    await reloaded.load_persisted_stats()
    task = reloaded._task_stats["search_wanted"]
    assert task.last_execution == datetime(2026, 4, 22, 6, 0, 0, tzinfo=UTC).isoformat()
    assert task.last_duration_seconds == 9.5
    assert task.last_status == "failed"
    assert task.last_missed_at == datetime(2026, 4, 22, 5, 30, 0, tzinfo=UTC).isoformat()
    assert task.missed_count == 3
    assert task.last_overlap_at == datetime(2026, 4, 22, 5, 45, 0, tzinfo=UTC).isoformat()
    assert task.overlap_count == 5
    assert task.last_exclusive_block_at == datetime(2026, 4, 22, 5, 50, 0, tzinfo=UTC).isoformat()
    assert task.exclusive_block_count == 7
    assert not sidecar_path.exists()

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "search_wanted")
        assert row is not None
        assert row.last_execution == datetime(2026, 4, 22, 6, 0, 0, tzinfo=UTC)
        assert row.last_duration_seconds == 9.5
        assert row.last_status == "failed"
        assert row.last_missed_at == datetime(2026, 4, 22, 5, 30, 0, tzinfo=UTC)
        assert row.missed_count == 3
        assert row.last_overlap_at == datetime(2026, 4, 22, 5, 45, 0, tzinfo=UTC)
        assert row.overlap_count == 5
        assert row.last_exclusive_block_at == datetime(2026, 4, 22, 5, 50, 0, tzinfo=UTC)
        assert row.exclusive_block_count == 7
        assert await session.get(ScheduledTaskStat, "unknown_task") is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_load_persisted_stats_prunes_unknown_db_rows(tmp_path, monkeypatch) -> None:
    """Unknown persisted task rows should be ignored and cleaned up."""
    db_file = tmp_path / "scheduler_prune.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": db_url})(),
    )

    async with factory() as session:
        session.add_all(
            [
                ScheduledTaskStat(task_id="search_wanted", last_status="completed"),
                ScheduledTaskStat(task_id="stale_task", last_status="completed"),
            ]
        )
        await session.commit()

    scheduler = PullboxScheduler()
    await scheduler.load_persisted_stats()

    assert "search_wanted" in scheduler._task_stats
    assert "stale_task" not in scheduler._task_stats

    async with factory() as session:
        assert await session.get(ScheduledTaskStat, "search_wanted") is not None
        assert await session.get(ScheduledTaskStat, "stale_task") is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_task_scheduled_success_persists_on_coarse_interval(
    tmp_path,
    monkeypatch,
) -> None:
    """Hot scheduled tasks should only persist routine success heartbeats every five minutes."""
    db_file = tmp_path / "scheduler_hot_success.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": db_url})(),
    )

    now = {"value": 1_700_000_000.0}
    monkeypatch.setattr("pullbox.core.scheduler.time.time", lambda: now["value"])

    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3

    stats = TaskStats(
        last_execution=_iso_from_epoch(now["value"]),
        last_duration_seconds=1.23,
        last_status="completed",
    )
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_execution == datetime.fromtimestamp(1_700_000_000.0, UTC)

    now["value"] += 60
    stats.last_execution = _iso_from_epoch(now["value"])
    stats.last_duration_seconds = 2.34
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_execution == datetime.fromtimestamp(1_700_000_000.0, UTC)
        assert row.last_duration_seconds == 1.23

    now["value"] += 301
    stats.last_execution = _iso_from_epoch(now["value"])
    stats.last_duration_seconds = 3.45
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_execution == datetime.fromtimestamp(now["value"], UTC)
        assert row.last_duration_seconds == 3.45

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_task_failure_recovery_and_manual_runs_persist_immediately(
    tmp_path,
    monkeypatch,
) -> None:
    """Failures, recovery successes, and manual runs should bypass hot-task throttling."""
    db_file = tmp_path / "scheduler_hot_immediate.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": db_url})(),
    )

    now = {"value": 1_800_000_000.0}
    monkeypatch.setattr("pullbox.core.scheduler.time.time", lambda: now["value"])

    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3

    stats = TaskStats(
        last_execution=_iso_from_epoch(now["value"]),
        last_duration_seconds=1.0,
        last_status="completed",
    )
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    now["value"] += 60
    stats.last_execution = _iso_from_epoch(now["value"])
    stats.last_duration_seconds = 2.0
    stats.last_status = "failed"
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="failed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_status == "failed"
        assert row.last_execution == datetime.fromtimestamp(now["value"], UTC)

    now["value"] += 60
    stats.last_execution = _iso_from_epoch(now["value"])
    stats.last_duration_seconds = 3.0
    stats.last_status = "completed"
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_status == "completed"
        assert row.last_execution == datetime.fromtimestamp(now["value"], UTC)

    now["value"] += 60
    stats.last_execution = _iso_from_epoch(now["value"])
    stats.last_duration_seconds = 4.0
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="manual",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.last_execution == datetime.fromtimestamp(now["value"], UTC)
        assert row.last_duration_seconds == 4.0

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_task_overlap_and_missed_counters_flush_on_coarse_interval(
    tmp_path,
    monkeypatch,
) -> None:
    """Hot-task overlap and missed counters should aggregate in memory until the coarse flush."""
    db_file = tmp_path / "scheduler_hot_events.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": db_url})(),
    )

    now = {"value": 1_900_000_000.0}
    monkeypatch.setattr("pullbox.core.scheduler.time.time", lambda: now["value"])

    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3

    stats = TaskStats(
        last_missed_at=_iso_from_epoch(now["value"]),
        missed_count=1,
        last_status="completed",
    )
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="missed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is None

    now["value"] += 60
    stats.last_overlap_at = _iso_from_epoch(now["value"])
    stats.overlap_count = 1
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="overlap",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is None

    now["value"] += 60
    stats.last_missed_at = _iso_from_epoch(now["value"])
    stats.missed_count = 2
    stats.last_overlap_at = _iso_from_epoch(now["value"])
    stats.overlap_count = 2
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="missed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is None

    now["value"] += 300
    stats.last_execution = _iso_from_epoch(now["value"])
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="completed",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.missed_count == 2
        assert row.overlap_count == 2

    now["value"] += 301
    stats.last_overlap_at = _iso_from_epoch(now["value"])
    await scheduler._persist_task_stat(
        "monitor_downloads",
        stats,
        trigger_type="scheduled",
        reason="overlap",
    )

    async with factory() as session:
        row = await session.get(ScheduledTaskStat, "monitor_downloads")
        assert row is not None
        assert row.missed_count == 2
        assert row.overlap_count == 2
        assert row.last_missed_at == datetime.fromtimestamp(1_900_000_120.0, UTC)
        assert row.last_overlap_at == datetime.fromtimestamp(1_900_000_120.0, UTC)

    await engine.dispose()


def test_hot_task_overlap_logs_at_debug(monkeypatch) -> None:
    """Hot task overlap events should stay out of warning-level operator logs."""
    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3

    warning = MagicMock()
    debug = MagicMock()
    monkeypatch.setattr("pullbox.core.scheduler.logger.warning", warning)
    monkeypatch.setattr("pullbox.core.scheduler.logger.debug", debug)

    scheduler._log_task_overlap("monitor_downloads", trigger_type="scheduled")

    warning.assert_not_called()
    debug.assert_called_once_with(
        "task_skipped_overlap",
        task_id="monitor_downloads",
        trigger_type="scheduled",
    )


def test_non_hot_task_overlap_logs_at_warning(monkeypatch) -> None:
    """Normal scheduled jobs should still surface overlap problems at warning level."""
    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["search_wanted"] = 3600

    warning = MagicMock()
    debug = MagicMock()
    monkeypatch.setattr("pullbox.core.scheduler.logger.warning", warning)
    monkeypatch.setattr("pullbox.core.scheduler.logger.debug", debug)

    scheduler._log_task_overlap("search_wanted")

    debug.assert_not_called()
    warning.assert_called_once_with(
        "task_skipped_overlap",
        task_id="search_wanted",
    )


def test_missing_persisted_table_warning_is_actionable_and_deduped(monkeypatch) -> None:
    """Missing-table warnings should include the table, DB, phase, and remediation hint."""
    scheduler = PullboxScheduler()
    monkeypatch.setattr(
        scheduler, "_runtime_db_url", lambda: "sqlite+aiosqlite:////data/pullbox.db"
    )
    captured: list[dict[str, object]] = []

    def _capture(logger_obj, event: str, **kwargs: object) -> None:  # type: ignore[no-untyped-def]
        captured.append({"logger": logger_obj, "event": event, **kwargs})

    monkeypatch.setattr("pullbox.core.scheduler.log_deduped_warning", _capture)

    scheduler._log_missing_persisted_table(
        task_id="monitor_downloads",
        table_name="scheduled_task_stats",
        phase="stats",
    )

    assert len(captured) == 1
    warning = captured[0]
    assert warning["event"] == "scheduler_persisted_table_missing"
    assert warning["task_id"] == "monitor_downloads"
    assert warning["table_name"] == "scheduled_task_stats"
    assert warning["db_url"] == "sqlite+aiosqlite:////data/pullbox.db"
    assert warning["phase"] == "stats"
    assert "Apply the latest migrations and restart the app" in str(warning["action_required"])


@pytest.mark.asyncio
async def test_load_persisted_stats_uses_passed_session(
    async_engine,
    monkeypatch,
) -> None:
    """Passing a session should hydrate stats without opening a new session factory."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    async with factory() as session:
        session.add(
            ScheduledTaskStat(
                task_id="refresh_dashboard_intelligence",
                last_execution=datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC),
                last_duration_seconds=4.5,
                last_status="completed",
            )
        )
        await session.commit()

    scheduler = PullboxScheduler()
    async with factory() as session:
        await scheduler.load_persisted_stats(session)

    assert scheduler._task_stats["refresh_dashboard_intelligence"].last_status == "completed"


def test_run_task_now_returns_none_for_unknown_task() -> None:
    """Unknown task ids should not enqueue phantom manual runs."""
    scheduler = PullboxScheduler()
    assert scheduler.run_task_now("does_not_exist") is None


def test_get_jobs_returns_registered_job_summaries() -> None:
    """The jobs summary helper should expose APScheduler registration info."""
    scheduler = PullboxScheduler()

    async def _noop() -> None:
        return None

    scheduler.add_job(_noop, "interval", task_id="job_summary_task", hours=1)
    jobs = scheduler.get_jobs()

    assert len(jobs) == 1
    assert jobs[0]["id"] == "job_summary_task"
    assert jobs[0]["name"] == "job_summary_task"
    assert jobs[0]["trigger"] == "interval[1:00:00]"
    assert "next_run_time" in jobs[0]


@pytest.mark.asyncio
async def test_get_scheduled_tasks_skips_manual_one_shot_jobs() -> None:
    """Manual one-shot APScheduler jobs should stay hidden from the Tasks page rows."""
    scheduler = PullboxScheduler()

    async def _noop() -> None:
        return None

    scheduler.add_job(_noop, "interval", task_id="visible_task", hours=1)
    scheduler.add_job(_noop, "date", task_id="hidden_task_manual")

    task_rows = scheduler.get_scheduled_tasks()
    assert [row["task_id"] for row in task_rows] == ["visible_task"]


@pytest.mark.asyncio
async def test_failed_run_updates_scheduler_task_state(monkeypatch) -> None:
    """Failed task runs should persist the failed status and clear running state."""
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )
    scheduler = PullboxScheduler()
    scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]

    async def _boom() -> None:
        raise RuntimeError("boom")

    wrapped = scheduler._wrap_task(_boom, "test_failed_task")
    await wrapped()

    stats = scheduler._task_stats["test_failed_task"]
    assert stats.last_status == "failed"
    assert stats.running_since is None
    assert "test_failed_task" not in scheduler._running_counts


@pytest.mark.asyncio
async def test_persist_task_stat_missing_table_logs_and_returns(
    async_engine,
    monkeypatch,
) -> None:
    """Missing-table persistence failures should log an actionable warning and stop."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    scheduler = PullboxScheduler()
    scheduler._upsert_task_stat_row = AsyncMock(  # type: ignore[method-assign]
        side_effect=OperationalError(
            "INSERT",
            {},
            Exception("no such table: scheduled_task_stats"),
        )
    )
    scheduler._log_missing_persisted_table = MagicMock()  # type: ignore[method-assign]

    await scheduler._persist_task_stat("monitor_downloads", TaskStats(last_status="completed"))

    scheduler._log_missing_persisted_table.assert_called_once()


@pytest.mark.asyncio
async def test_persist_task_stat_retries_locked_db_then_succeeds(
    async_engine,
    monkeypatch,
) -> None:
    """Transient SQLite locks should retry before persisting successfully."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    scheduler = PullboxScheduler()
    calls = {"count": 0}

    async def _upsert(*_args, **_kwargs) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))

    sleep = AsyncMock()
    monkeypatch.setattr("pullbox.core.scheduler.asyncio.sleep", sleep)
    scheduler._upsert_task_stat_row = AsyncMock(side_effect=_upsert)  # type: ignore[method-assign]

    await scheduler._persist_task_stat("monitor_downloads", TaskStats(last_status="completed"))

    assert calls["count"] == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_task_stat_drops_locked_hot_task_metadata(
    async_engine,
    monkeypatch,
) -> None:
    """Hot-task stat writes should fail soft when SQLite is busy."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3
    scheduler._upsert_task_stat_row = AsyncMock(  # type: ignore[method-assign]
        side_effect=OperationalError("INSERT", {}, Exception("database is locked"))
    )

    sleep = AsyncMock()
    warning = MagicMock()
    debug = MagicMock()
    monkeypatch.setattr("pullbox.core.scheduler.asyncio.sleep", sleep)
    monkeypatch.setattr("pullbox.core.scheduler.logger.warning", warning)
    monkeypatch.setattr("pullbox.core.scheduler.logger.debug", debug)

    await scheduler._persist_task_stat("monitor_downloads", TaskStats(last_status="completed"))

    assert "monitor_downloads" in scheduler._task_last_persisted_at
    warning.assert_not_called()
    debug.assert_any_call(
        "scheduler_task_stats_persist_skipped_locked",
        task_id="monitor_downloads",
    )
    assert sleep.await_count == 7


@pytest.mark.asyncio
async def test_persist_task_stat_disables_persistence_on_unusable_store(
    async_engine,
    monkeypatch,
) -> None:
    """Malformed/disk-I/O scheduler stats failures should fail dark after one warning."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)

    scheduler = PullboxScheduler()
    scheduler._upsert_task_stat_row = AsyncMock(  # type: ignore[method-assign]
        side_effect=DatabaseError(
            "INSERT",
            {},
            Exception("database disk image is malformed"),
        )
    )
    disable = MagicMock()
    scheduler._disable_task_stats_persistence = disable  # type: ignore[method-assign]

    await scheduler._persist_task_stat("monitor_downloads", TaskStats(last_status="completed"))

    disable.assert_called_once()


@pytest.mark.asyncio
async def test_persist_task_stat_returns_immediately_when_disabled(monkeypatch) -> None:
    """Once stats persistence is disabled, hot tasks should stop touching the DB."""
    scheduler = PullboxScheduler()
    scheduler._task_stats_persistence_disabled = True
    factory = MagicMock(side_effect=AssertionError("session factory should not be called"))
    monkeypatch.setattr("pullbox.database.get_session_factory", factory)

    await scheduler._persist_task_stat("monitor_downloads", TaskStats(last_status="completed"))

    factory.assert_not_called()


def test_helper_methods_cover_hot_task_persistence_and_singleton(monkeypatch) -> None:
    """Scheduler helper branches should behave predictably across hot-task decisions."""
    scheduler = PullboxScheduler()
    scheduler._task_interval_seconds["monitor_downloads"] = 3
    scheduler._task_last_persisted_at["monitor_downloads"] = 1_000.0
    scheduler._task_last_persisted_status["monitor_downloads"] = "completed"
    monkeypatch.setattr("pullbox.core.scheduler.time.time", lambda: 1_100.0)

    assert scheduler._is_hot_task("monitor_downloads") is True
    assert scheduler._coarse_persist_due("monitor_downloads") is False
    assert (
        scheduler._should_persist_task_stat(
            "monitor_downloads",
            TaskStats(last_status="completed"),
            trigger_type="scheduled",
            reason="completed",
        )
        is False
    )
    assert (
        scheduler._should_persist_task_stat(
            "monitor_downloads",
            TaskStats(last_status="completed"),
            trigger_type="manual",
            reason="completed",
        )
        is True
    )
    assert (
        scheduler._should_persist_task_stat(
            "monitor_downloads",
            TaskStats(last_status="completed"),
            trigger_type="scheduled",
            reason="overlap",
        )
        is False
    )
    assert (
        scheduler._should_persist_task_stat(
            "monitor_downloads",
            TaskStats(last_status="completed"),
            trigger_type="scheduled",
            reason="exclusive_block",
        )
        is False
    )
    from pullbox.core import scheduler as scheduler_module

    scheduler_module._scheduler_instance = None
    first = scheduler_module.get_scheduler()
    second = scheduler_module.get_scheduler()
    assert first is second


@pytest.mark.asyncio
async def test_exclusive_block_stats_round_trip_through_rows(monkeypatch) -> None:
    """Exclusive-task block metadata should be visible through scheduler task rows."""
    _task_registry.clear()
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:///:memory:"})(),
    )

    @scheduled_task(
        task_id="test_exclusive_block_task",
        trigger="interval",
        display_name="Test Exclusive Block Task",
        hours=1,
    )
    async def _task() -> None:
        return None

    scheduler = PullboxScheduler()
    scheduler.register_tasks()
    scheduler.start()
    try:
        stats = scheduler._task_stats["test_exclusive_block_task"]
        stats.last_exclusive_block_at = datetime.now(UTC).isoformat()
        stats.exclusive_block_count = 2

        task = scheduler.get_scheduled_tasks()[0]
        assert task["last_exclusive_block_at"] == stats.last_exclusive_block_at
        assert task["exclusive_block_count"] == 2
    finally:
        scheduler.shutdown(wait=False)
        _task_registry.clear()


def test_runtime_db_url_prefers_bound_engine_url(async_engine, monkeypatch) -> None:
    """The runtime DB URL helper should prefer the bound engine URL when available."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: factory)
    monkeypatch.setattr(
        "pullbox.core.scheduler.get_settings",
        lambda: type("Settings", (), {"db_url": "sqlite+aiosqlite:////fallback.db"})(),
    )

    scheduler = PullboxScheduler()
    assert str(async_engine.url) == scheduler._runtime_db_url()
