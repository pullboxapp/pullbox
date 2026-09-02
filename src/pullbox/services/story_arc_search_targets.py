"""Bounded canonical targets for an explicitly requested story-arc search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select

from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.search_targets import (
    IssueSearchTarget,
    _target_from_row,
    arc_issue_acquisition_filter,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def story_arc_search_ceiling(session: AsyncSession, story_arc_id: int) -> int:
    """Freeze a finite issue-ID ceiling without materializing the arc catalog."""
    result = await session.scalar(
        select(func.max(IssueStoryArc.issue_id)).where(IssueStoryArc.story_arc_id == story_arc_id)
    )
    return int(result or 0)


async def load_story_arc_search_eligible_counts(
    session: AsyncSession,
    story_arc_ids: Sequence[int],
) -> dict[int, int]:
    """Count currently searchable missing issues for bounded registry rows."""
    if not story_arc_ids:
        return {}
    if len(story_arc_ids) > 100:
        raise ValueError("Story Arc search eligibility is limited to 100 visible arcs")
    result = await session.execute(
        select(IssueStoryArc.story_arc_id, func.count(func.distinct(Issue.id)))
        .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
        .join(Issue, Issue.id == IssueStoryArc.issue_id)
        .where(
            IssueStoryArc.story_arc_id.in_(story_arc_ids),
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
            StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
            arc_issue_acquisition_filter(),
        )
        .group_by(IssueStoryArc.story_arc_id)
    )
    return {int(story_arc_id): int(count) for story_arc_id, count in result.all()}


async def load_story_arc_missing_search_targets(
    session: AsyncSession,
    story_arc_id: int,
    *,
    issue_ids: Sequence[int] | None = None,
    series_id: int | None = None,
    after_issue_id: int = 0,
    ceiling_issue_id: int | None = None,
    limit: int = 100,
) -> list[IssueSearchTarget]:
    """Search only resolved, missing arc members while preserving explicit skips.

    Manual arc searches need not enable whole-series or arc monitoring. They
    still respect publication dates, existing files, active downloads,
    unresolved memberships, and pending intervention decisions.
    """
    if not 1 <= limit <= 100 or after_issue_id < 0:
        raise ValueError("Story Arc search pages require a limit of 1-100 and a valid cursor")
    if issue_ids is not None and (not issue_ids or len(issue_ids) > 100):
        if not issue_ids:
            return []
        raise ValueError("Story Arc search ID batches must not exceed 100")
    membership = exists().where(
        IssueStoryArc.story_arc_id == story_arc_id,
        IssueStoryArc.issue_id == Issue.id,
        IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        StoryArc.id == IssueStoryArc.story_arc_id,
        StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
    )
    statement = (
        select(
            Issue.id.label("issue_id"),
            Issue.series_id.label("series_id"),
            Issue.issue_number.label("issue_number"),
            Issue.issue_number_text.label("issue_number_text"),
            Issue.issue_type.label("issue_type"),
            Issue.title.label("issue_title"),
            Issue.release_date.label("release_date"),
            Issue.store_date.label("store_date"),
            Series.title.label("series_title"),
            Series.year_start.label("series_year"),
            Series.alternate_names.label("alternate_names"),
            Series.issue_count.label("series_issue_count"),
        )
        .join(Series, Series.id == Issue.series_id)
        .where(
            Issue.id > after_issue_id,
            arc_issue_acquisition_filter(),
            membership,
        )
        .order_by(Issue.id)
        .limit(limit)
    )
    if issue_ids is not None:
        statement = statement.where(Issue.id.in_(issue_ids))
    if series_id is not None:
        statement = statement.where(Issue.series_id == series_id)
    if ceiling_issue_id is not None:
        statement = statement.where(Issue.id <= ceiling_issue_id)
    return [_target_from_row(row) for row in (await session.execute(statement)).all()]
