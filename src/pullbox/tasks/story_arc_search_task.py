"""Explicit arc-member searches reuse the shared acquisition runner in small batches."""

from __future__ import annotations

import asyncio
from collections import defaultdict

import structlog

from pullbox.database import get_session_factory
from pullbox.services.story_arc_search_targets import (
    load_story_arc_missing_search_targets,
    story_arc_search_ceiling,
)
from pullbox.tasks.search_task import search_series_issues

logger = structlog.get_logger(__name__)
_PAGE_SIZE = 50
_running_searches: dict[int, asyncio.Task[dict[str, int]]] = {}


async def search_story_arc_issues(story_arc_id: int) -> dict[str, int]:
    """Search the finite current arc without monitoring any parent series."""
    totals = {"wanted": 0, "sent": 0, "queued": 0}
    factory = get_session_factory()
    async with factory() as session:
        ceiling = await story_arc_search_ceiling(session, story_arc_id)
    cursor = 0
    while cursor < ceiling:
        async with factory() as session:
            targets = await load_story_arc_missing_search_targets(
                session,
                story_arc_id,
                after_issue_id=cursor,
                ceiling_issue_id=ceiling,
                limit=_PAGE_SIZE,
            )
        if not targets:
            break
        cursor = targets[-1].issue_id
        by_series: dict[int, list[int]] = defaultdict(list)
        for target in targets:
            by_series[target.series_id].append(target.issue_id)
        for series_id, issue_ids in by_series.items():
            result = await search_series_issues(
                series_id,
                story_arc_id=story_arc_id,
                issue_ids=issue_ids,
            )
            for name in totals:
                totals[name] += result[name]
        await asyncio.sleep(0)
    logger.info("story_arc_search_completed", story_arc_id=story_arc_id, **totals)
    return totals


def _search_finished(story_arc_id: int, task: asyncio.Task[dict[str, int]]) -> None:
    if _running_searches.get(story_arc_id) is task:
        _running_searches.pop(story_arc_id, None)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("story_arc_search_failed", story_arc_id=story_arc_id)


def schedule_story_arc_search(story_arc_id: int) -> bool:
    """Keep one live explicit search per arc in the application worker."""
    existing = _running_searches.get(story_arc_id)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(search_story_arc_issues(story_arc_id))
    _running_searches[story_arc_id] = task
    task.add_done_callback(lambda done: _search_finished(story_arc_id, done))
    return True
