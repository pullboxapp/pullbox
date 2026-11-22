"""Read-only impact of removing the parent series of confirmed arc members."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc
from pullbox.services.story_arc_member_review import registered_file_page, resolved_member_issue_ids
from pullbox.services.story_arc_service import StoryArcNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_PAGE_SIZE = 20


def _page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "items": items[:_PAGE_SIZE],
        "next_after": items[_PAGE_SIZE - 1]["id"] if len(items) > _PAGE_SIZE else None,
    }


async def related_series(db: AsyncSession, arc_id: int, after: int) -> dict[str, Any]:
    if await db.get(StoryArc, arc_id) is None:
        raise StoryArcNotFoundError("Story Arc not found.")
    # First page exact parent IDs; then count independent relations so alternate
    # files and memberships in several arcs never multiply the issue totals.
    parents = (
        select(Issue.series_id.label("id"), func.count().label("members"))
        .where(Issue.id.in_(resolved_member_issue_ids(arc_id)), Issue.series_id > after)
        .group_by(Issue.series_id)
        .order_by(Issue.series_id)
        .limit(_PAGE_SIZE + 1)
        .cte()
    )
    total = (
        select(func.count(Issue.id))
        .where(Issue.series_id == Series.id)
        .correlate(Series)
        .scalar_subquery()
    )
    files = (
        select(func.count(LibraryFile.id))
        .select_from(Issue)
        .join(LibraryFile, LibraryFile.issue_id == Issue.id)
        .where(Issue.series_id == Series.id)
        .correlate(Series)
        .scalar_subquery()
    )
    peers = (
        select(func.count(func.distinct(IssueStoryArc.story_arc_id)))
        .select_from(Issue)
        .join(IssueStoryArc, IssueStoryArc.issue_id == Issue.id)
        .where(Issue.series_id == Series.id, IssueStoryArc.story_arc_id != arc_id)
        .correlate(Series)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(
            Series.id,
            Series.title,
            Series.year_start.label("year"),
            parents.c.members,
            (total - parents.c.members).label("outside"),
            files.label("files"),
            peers.label("other_arcs"),
        )
        .join(parents, parents.c.id == Series.id)
        .order_by(Series.id)
    )
    return _page([dict(row) for row in rows.mappings()])


async def _require_parent(db: AsyncSession, arc_id: int, series_id: int) -> None:
    if not await db.scalar(
        select(
            select(Issue.id)
            .where(Issue.series_id == series_id, Issue.id.in_(resolved_member_issue_ids(arc_id)))
            .exists()
        )
    ):
        raise StoryArcNotFoundError("Series has no confirmed issue in this Story Arc.")


async def outside_issues(
    db: AsyncSession, arc_id: int, series_id: int, after: int
) -> dict[str, Any]:
    await _require_parent(db, arc_id, series_id)
    members = resolved_member_issue_ids(arc_id).where(IssueStoryArc.issue_id.is_not(None))
    rows = await db.execute(
        select(Issue.id, Issue.issue_number_text, Issue.issue_number, Issue.title)
        .where(Issue.series_id == series_id, Issue.id > after, Issue.id.not_in(members))
        .order_by(Issue.id)
        .limit(_PAGE_SIZE + 1)
    )
    return _page(
        [
            {
                "id": r.id,
                "number": r.issue_number_text or format_issue_number(r.issue_number),
                "title": r.title,
            }
            for r in rows
        ]
    )


async def series_files(
    db: AsyncSession, arc_id: int, series_id: int, after: int
) -> dict[str, object]:
    await _require_parent(db, arc_id, series_id)
    return await registered_file_page(db, arc_id, after, series_id=series_id)


async def series_peers(db: AsyncSession, arc_id: int, series_id: int, after: int) -> dict[str, Any]:
    await _require_parent(db, arc_id, series_id)
    # An unresolved entry with a saved issue FK also loses that link if the
    # series goes away. It counts as an impact, never as authorization.
    rows = await db.execute(
        select(
            StoryArc.id,
            StoryArc.name,
            StoryArc.lifecycle,
            func.count(IssueStoryArc.id).label("linked"),
        )
        .select_from(Issue)
        .join(IssueStoryArc, IssueStoryArc.issue_id == Issue.id)
        .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
        .where(Issue.series_id == series_id, StoryArc.id != arc_id, StoryArc.id > after)
        .group_by(StoryArc.id, StoryArc.name, StoryArc.lifecycle)
        .order_by(StoryArc.id)
        .limit(_PAGE_SIZE + 1)
    )
    return _page(
        [dict(id=r.id, name=r.name, lifecycle=r.lifecycle.value, linked=r.linked) for r in rows]
    )
