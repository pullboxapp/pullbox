"""Bounded canonical targets for an explicitly requested story-arc search."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import and_, exists, func, or_, select

from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.search_targets import IssueSearchTarget, _target_from_row

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def story_arc_search_ceiling(session: AsyncSession, story_arc_id: int) -> int:
    """Freeze a finite issue-ID ceiling without materializing the arc catalog."""
    result = await session.scalar(
        select(func.max(IssueStoryArc.issue_id)).where(IssueStoryArc.story_arc_id == story_arc_id)
    )
    return int(result or 0)


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
    still respect the arc's upcoming option, existing files, active downloads,
    unresolved memberships, and pending intervention decisions.
    """
    if not 1 <= limit <= 100 or after_issue_id < 0:
        raise ValueError("Story Arc search pages require a limit of 1-100 and a valid cursor")
    if issue_ids is not None and (not issue_ids or len(issue_ids) > 100):
        if not issue_ids:
            return []
        raise ValueError("Story Arc search ID batches must not exceed 100")
    today = date.today()
    # Comic Vine's release_date is the cover date, often months after stores
    # receive an issue. Prefer a known store date; today's releases are missing,
    # not upcoming, and an undated issue must not be excluded by SQL NULL logic.
    publication_date = func.coalesce(Issue.store_date, Issue.release_date)
    upcoming = and_(publication_date.is_not(None), publication_date > today)
    membership = exists().where(
        IssueStoryArc.story_arc_id == story_arc_id,
        IssueStoryArc.issue_id == Issue.id,
        IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        StoryArc.id == IssueStoryArc.story_arc_id,
        StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
        or_(StoryArc.include_upcoming.is_(True), ~upcoming),
    )
    active_download = exists().where(
        DownloadHistory.issue_id == Issue.id,
        or_(
            DownloadHistory.state.in_(
                (
                    DownloadState.QUEUED,
                    DownloadState.SENT,
                    DownloadState.DOWNLOADING,
                    DownloadState.FINALIZING,
                    DownloadState.PAUSED,
                    DownloadState.RETRY_PENDING,
                    DownloadState.POST_PROCESSING,
                )
            ),
            and_(
                DownloadHistory.state == DownloadState.COMPLETED,
                DownloadHistory.imported_at.is_(None),
            ),
        ),
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
            Issue.status.in_((IssueStatus.WANTED, IssueStatus.SKIPPED)),
            Issue.manual_skip.is_(False),
            membership,
            ~exists().where(LibraryFile.issue_id == Issue.id),
            ~active_download,
            ~exists().where(
                PendingMatch.issue_id == Issue.id,
                PendingMatch.status == PendingMatchStatus.PENDING,
            ),
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
