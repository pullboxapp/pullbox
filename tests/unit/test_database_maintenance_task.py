"""Tests for scheduled SQLite database maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from pullbox.core.scheduler import TaskExecutionResult
from pullbox.core.scheduler_registry import task_registry
from pullbox.tasks import database_maintenance_task

if TYPE_CHECKING:
    from pathlib import Path


def test_database_maintenance_task_is_nightly_and_exclusive() -> None:
    task = next(task for task in task_registry if task.task_id == "maintain_database")

    assert task.display_name == "Database Maintenance"
    assert task.trigger == "cron"
    assert task.trigger_kwargs == {"hour": 4, "minute": 30}
    assert task.exclusive is True


@pytest.mark.asyncio
async def test_database_maintenance_task_runs_for_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pullbox.db"
    db_path.touch()
    calls: list[Path] = []

    @dataclass(frozen=True)
    class Result:
        integrity_result: str = "ok"

    class Runtime:
        def __init__(self, path: Path) -> None:
            calls.append(path)

        async def maintain(self) -> Result:
            return Result()

    monkeypatch.setattr(
        database_maintenance_task,
        "get_settings",
        lambda: SimpleNamespace(db_url=f"sqlite+aiosqlite:///{db_path}"),
    )
    monkeypatch.setattr(database_maintenance_task, "DatabaseOptimizationRuntimeService", Runtime)

    result = await database_maintenance_task.maintain_database()

    assert result == TaskExecutionResult(status="completed")
    assert calls == [db_path]


@pytest.mark.asyncio
async def test_database_maintenance_task_skips_non_sqlite_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database_maintenance_task,
        "get_settings",
        lambda: SimpleNamespace(db_url="postgresql+asyncpg://pullbox@example/pullbox"),
    )

    result = await database_maintenance_task.maintain_database()

    assert result == TaskExecutionResult(status="completed")
