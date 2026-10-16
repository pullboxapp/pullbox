"""Optional arc-sync wakeups must stay inside the scheduler lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core.scheduler import PullboxScheduler
from pullbox.core.scheduler_registry import RegisteredTask
from pullbox.services.story_arc_sync_queue import (
    STORY_ARC_SYNC_TASK_ID,
    request_story_arc_sync_now,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
def sync() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
async def scheduler(
    monkeypatch: pytest.MonkeyPatch, sync: AsyncMock
) -> AsyncGenerator[PullboxScheduler, None]:
    scheduler = PullboxScheduler()
    monkeypatch.setattr("pullbox.core.scheduler.get_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "pullbox.core.scheduler._task_registry",
        [RegisteredTask(sync, STORY_ARC_SYNC_TASK_ID, "interval", {"hours": 1})],
    )
    scheduler.register_tasks()
    # The lifecycle tests own the worker but do not exercise runtime DB persistence.
    monkeypatch.setattr(scheduler, "_defer_task_for_import", AsyncMock(return_value=False))
    monkeypatch.setattr(scheduler, "_persist_task_stat", AsyncMock())
    try:
        yield scheduler
    finally:
        worker = scheduler._manual_worker
        scheduler.shutdown(wait=False)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.parametrize("previously_started", [False, True], ids=["before-start", "after-stop"])
async def test_sync_nudge_does_not_start_background_work_outside_lifecycle(
    scheduler: PullboxScheduler,
    sync: AsyncMock,
    previously_started: bool,
) -> None:
    if previously_started:
        scheduler.start()
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)

    request_story_arc_sync_now()

    assert scheduler._manual_worker is None, "Stopped scheduler must not create a DB-owning worker"
    assert not scheduler._manual_queue
    sync.assert_not_awaited()


async def test_sync_nudge_still_runs_once_while_scheduler_is_started(
    scheduler: PullboxScheduler,
    sync: AsyncMock,
) -> None:
    scheduler.start()

    request_story_arc_sync_now()
    request_story_arc_sync_now()

    worker = scheduler._manual_worker
    assert worker is not None
    await asyncio.wait_for(worker, timeout=5)
    sync.assert_awaited_once_with()
    assert scheduler._manual_worker is None


async def test_sync_nudge_remains_best_effort_when_running_scheduler_rejects_it(
    scheduler: PullboxScheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler.start()

    def unavailable(_task_id: str) -> None:
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(scheduler, "run_task_now", unavailable)
    request_story_arc_sync_now()
    assert scheduler._manual_worker is None
