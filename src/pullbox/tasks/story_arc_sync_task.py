"""Scheduled bounded drain for durable story-arc synchronization work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from pullbox.core.scheduler import get_scheduler, scheduled_task
from pullbox.services.import_activity import has_active_import_scheduler_protection
from pullbox.services.story_arc_sync_queue import (
    STORY_ARC_SYNC_TASK_ID,
    process_story_arc_sync_work,
)

logger = structlog.get_logger(__name__)


@scheduled_task(
    task_id=STORY_ARC_SYNC_TASK_ID,
    trigger="interval",
    display_name="Synchronize Story Arc Placements",
    seconds=300,
)
async def scheduled_sync_story_arc_placements() -> None:
    """Process one bounded batch and schedule the next durable continuation."""
    import_only = await has_active_import_scheduler_protection()
    result = await process_story_arc_sync_work(import_only=import_only)
    if result.import_jobs_evaluated:
        from pullbox.tasks.import_task import publish_story_arc_import_updates

        await publish_story_arc_import_updates(
            result.import_jobs_evaluated,
            completed_job_ids=result.import_jobs_completed,
        )
    if result.import_jobs_rollback_ready:
        from pullbox.tasks.import_task import trigger_import_rollback

        for job_id in result.import_jobs_rollback_ready:
            trigger_import_rollback(job_id)
    scheduler = get_scheduler()
    if result.has_more:
        scheduler.schedule_task_continuation(
            STORY_ARC_SYNC_TASK_ID,
            run_at=datetime.now(UTC) + timedelta(seconds=1),
            interval_seconds=300,
        )
    elif result.next_retry_at is not None:
        scheduler.schedule_task_continuation(
            STORY_ARC_SYNC_TASK_ID,
            run_at=result.next_retry_at,
            interval_seconds=300,
        )
    else:
        scheduler.clear_task_continuation(STORY_ARC_SYNC_TASK_ID)
    logger.info(
        "story_arc_sync_done",
        discovered=result.discovered,
        claimed=result.claimed,
        completed=result.completed,
        failed=result.failed,
        retrying=result.retrying,
        cancelled=result.cancelled,
        lost_claims=result.lost_claims,
        has_more=result.has_more,
        import_only=import_only,
        import_jobs_evaluated=len(result.import_jobs_evaluated),
        import_jobs_completed=len(result.import_jobs_completed),
        import_jobs_stalled=len(result.import_jobs_stalled),
        import_jobs_rollback_ready=len(result.import_jobs_rollback_ready),
    )
