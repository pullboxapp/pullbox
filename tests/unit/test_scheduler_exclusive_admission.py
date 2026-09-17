"""Exclusive maintenance must not indefinitely reserve the scheduler while waiting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pullbox.core.scheduler import PullboxScheduler, get_current_task_trigger_type


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> PullboxScheduler:
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("pullbox.core.scheduler._EXCLUSIVE_WAIT_SECONDS", 0.01, raising=False)
    result = PullboxScheduler()
    result._persist_task_stat = AsyncMock()
    return result


async def test_busy_exclusive_task_defers_without_blocking_other_jobs(
    scheduler: PullboxScheduler,
) -> None:
    scheduler._running_counts["sync_new_issues"] = 1
    backup = AsyncMock()
    task = asyncio.create_task(scheduler._wrap_task(backup, "run_backups", exclusive=True)())
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "Busy backup must yield instead of reserving the scheduler forever"
        await task
        backup.assert_not_awaited()
        assert scheduler._exclusive_active_task_id is None
        assert "run_backups" not in scheduler._running_counts
        assert scheduler._task_stats["run_backups"].last_status == "deferred"
        retry = scheduler._scheduler.get_job("run_backups__exclusive_retry")
        assert retry is not None
        assert retry not in scheduler._visible_jobs()

        ordinary = AsyncMock()
        await scheduler._wrap_task(ordinary, "monitor_downloads")()
        ordinary.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_waiting_exclusive_task_releases_reservation(
    scheduler: PullboxScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pullbox.core.scheduler._EXCLUSIVE_WAIT_SECONDS", 60, raising=False)
    scheduler._running_counts["sync_new_issues"] = 1
    backup = AsyncMock()
    task = asyncio.create_task(scheduler._wrap_task(backup, "run_backups", exclusive=True)())
    await asyncio.sleep(0)
    assert scheduler._exclusive_active_task_id == "run_backups"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler._exclusive_active_task_id is None
    assert "run_backups" not in scheduler._running_counts
    assert scheduler._running_counts["sync_new_issues"] == 1
    assert scheduler._scheduler.get_job("run_backups__exclusive_retry") is None
    backup.assert_not_awaited()


async def test_exclusive_retry_keeps_manual_intent_and_runs_after_work_drains(
    scheduler: PullboxScheduler,
) -> None:
    scheduler._running_counts["sync_new_issues"] = 1
    triggers: list[str] = []

    async def backup() -> None:
        triggers.append(get_current_task_trigger_type())

    task = asyncio.create_task(
        scheduler._wrap_task(backup, "run_backups", exclusive=True, trigger_type="manual")()
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "Manual backup must be rescheduled instead of waiting indefinitely"
        await task
        retry = scheduler._scheduler.get_job("run_backups__exclusive_retry")
        assert retry is not None
        scheduler._running_counts.pop("sync_new_issues")
        await retry.func()
        assert triggers == ["manual"]
        assert scheduler._task_stats["run_backups"].last_status == "completed"
        assert scheduler._exclusive_active_task_id is None
        assert scheduler._scheduler.get_job("run_backups__exclusive_retry") is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_duplicate_exclusive_run_cannot_enter_while_first_is_waiting(
    scheduler: PullboxScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pullbox.core.scheduler._EXCLUSIVE_WAIT_SECONDS", 60, raising=False)
    scheduler._running_counts["sync_new_issues"] = 1
    backup = AsyncMock()
    wrapped = scheduler._wrap_task(backup, "run_backups", exclusive=True)
    first = asyncio.create_task(wrapped())
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.sleep(0)
        second = asyncio.create_task(wrapped())
        done, _ = await asyncio.wait({second}, timeout=0.2)
        assert second in done, "An exclusive task waiting for admission is already reserved"
        await second
        assert scheduler._exclusive_active_task_id == "run_backups"
        assert scheduler._task_stats["run_backups"].overlap_count == 1
        scheduler._running_counts.pop("sync_new_issues")
        await asyncio.wait_for(first, timeout=1)
        backup.assert_awaited_once()
    finally:
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_exclusive_retry_is_not_lost_when_another_maintenance_task_is_active(
    scheduler: PullboxScheduler,
) -> None:
    scheduler._exclusive_active_task_id = "maintain_database"
    scheduler._running_counts["maintain_database"] = 1
    backup = AsyncMock()
    await scheduler._wrap_task(backup, "run_backups", exclusive=True)()
    retry = scheduler._scheduler.get_job("run_backups__exclusive_retry")
    assert retry is not None, "Exclusive contention must retain a later retry"
    assert scheduler._exclusive_active_task_id == "maintain_database"
    assert "run_backups" not in scheduler._running_counts
    backup.assert_not_awaited()
