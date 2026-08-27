"""Nightly SQLite index and query-planner maintenance."""

from __future__ import annotations

from pathlib import Path

import structlog

from pullbox.config import get_settings
from pullbox.core.scheduler import TaskExecutionResult, scheduled_task
from pullbox.services.database_optimization_service import (
    DatabaseOptimizationRuntimeService,
)

logger = structlog.get_logger(__name__)


def _sqlite_database_path(db_url: str) -> Path | None:
    """Return the file path for a file-backed SQLite URL."""
    if not db_url.startswith(("sqlite:///", "sqlite+aiosqlite:///")):
        return None
    raw_path = db_url.split(":///", 1)[1]
    if not raw_path or raw_path == ":memory:":
        return None
    return Path(raw_path)


@scheduled_task(
    task_id="maintain_database",
    trigger="cron",
    display_name="Database Maintenance",
    hour=4,
    minute=30,
    exclusive=True,
)
async def maintain_database() -> TaskExecutionResult:
    """Rebuild SQLite indexes and refresh query-planner statistics nightly."""
    db_path = _sqlite_database_path(get_settings().db_url)
    if db_path is None:
        logger.debug("nightly_database_maintenance_skipped", reason="not_file_backed_sqlite")
        return TaskExecutionResult(status="completed")

    result = await DatabaseOptimizationRuntimeService(db_path).maintain()
    logger.info(
        "nightly_database_maintenance_completed",
        integrity_result=result.integrity_result,
    )
    return TaskExecutionResult(status="completed")
