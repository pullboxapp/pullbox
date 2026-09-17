"""Thin scheduler wrappers for metadata refresh and new-issue sync."""

from __future__ import annotations

from pullbox.core.scheduler import TaskExecutionResult, scheduled_task
from pullbox.tasks.metadata_task import refresh_metadata, sync_new_issues


@scheduled_task(
    task_id="sync_new_issues",
    trigger="cron",
    display_name="Sync New Issues",
    hour=1,
    minute=0,
)
async def scheduled_sync_new_issues() -> TaskExecutionResult:
    """Run the monitored-series issue sync on its configured cadence."""
    return await sync_new_issues()


@scheduled_task(
    task_id="refresh_metadata",
    trigger="cron",
    display_name="Refresh Metadata",
    hour=3,
    minute=15,
)
async def scheduled_refresh_metadata() -> TaskExecutionResult:
    """Run the stale-series metadata refresh on its nightly cadence."""
    return await refresh_metadata()
