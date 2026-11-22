"""Read-only impact review for registered library comics in a Story Arc."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.import_job import ImportedFile
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.services.story_arc_service import StoryArcNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


_LIMIT = 20


def resolved_member_issue_ids(arc_id: int) -> Select[int | None]:
    return select(IssueStoryArc.issue_id).where(
        IssueStoryArc.story_arc_id == arc_id,
        IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
    )


async def member_files(db: AsyncSession, arc_id: int, after: int) -> dict[str, object]:
    if await db.scalar(select(StoryArc.id).where(StoryArc.id == arc_id)) is None:
        raise StoryArcNotFoundError("Story Arc not found.")
    return await registered_file_page(db, arc_id, after, series_id=None)


async def registered_file_page(
    db: AsyncSession, arc_id: int, after: int, *, series_id: int | None
) -> dict[str, object]:
    """Page registered paths after the caller has checked the arc or related series."""
    shared = (
        select(func.count(IssueStoryArc.id))
        .where(
            IssueStoryArc.issue_id == LibraryFile.issue_id,
            IssueStoryArc.story_arc_id != arc_id,
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
        .correlate(LibraryFile)
        .scalar_subquery()
    )
    history = select(ImportedFile.id).where(ImportedFile.library_file_id == LibraryFile.id).exists()
    rows = (
        await db.execute(
            select(
                LibraryFile.id,
                LibraryFile.file_path,
                LibraryFile.storage_mode,
                Series.title,
                Issue.issue_number_text,
                Issue.issue_number,
                LibraryRoot.enabled,
                LibraryRoot.allow_managed_writes,
                history.label("history"),
                shared.label("shared"),
            )
            .select_from(LibraryFile)
            .join(Issue, Issue.id == LibraryFile.issue_id)
            .join(Series, Series.id == Issue.series_id)
            .join(LibraryRoot, LibraryRoot.id == LibraryFile.library_root_id)
            .where(
                Issue.series_id == series_id
                if series_id is not None
                else LibraryFile.issue_id.in_(resolved_member_issue_ids(arc_id)),
                LibraryFile.id > after,
            )
            .order_by(LibraryFile.id)
            .limit(_LIMIT + 1)
        )
    ).all()
    return {
        "items": [
            dict(
                id=r.id,
                path=r.file_path,
                ownership=r.storage_mode.value,
                series=r.title,
                issue=r.issue_number_text or format_issue_number(r.issue_number),
                root_enabled=r.enabled,
                root_writable=r.allow_managed_writes,
                import_linked=r.history,
                shared=r.shared,
            )
            for r in rows[:_LIMIT]
        ],
        "next_after": rows[_LIMIT - 1].id if len(rows) > _LIMIT else None,
    }


async def sharing_arcs(
    db: AsyncSession, arc_id: int, file_id: int, after: int
) -> dict[str, object]:
    member_issue = select(LibraryFile.issue_id).where(
        LibraryFile.id == file_id, LibraryFile.issue_id.in_(resolved_member_issue_ids(arc_id))
    )
    if await db.scalar(member_issue) is None:
        raise StoryArcNotFoundError("Member file not found in this Story Arc.")
    rows = (
        await db.execute(
            select(StoryArc.id, StoryArc.name, StoryArc.lifecycle)
            .join(IssueStoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
            .where(
                IssueStoryArc.issue_id == member_issue.scalar_subquery(),
                IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                StoryArc.id != arc_id,
                StoryArc.id > after,
            )
            .order_by(StoryArc.id)
            .limit(_LIMIT + 1)
        )
    ).all()
    return {
        "items": [dict(id=r.id, name=r.name, lifecycle=r.lifecycle.value) for r in rows[:_LIMIT]],
        "next_after": rows[_LIMIT - 1].id if len(rows) > _LIMIT else None,
    }
