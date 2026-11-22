"""Preserve the reading orders affected by a series record deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, false, func, or_, select, update

from pullbox.core.exceptions import ValidationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementOwnership,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_membership_policy import retain_missing_chapter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def check_series_arc_work(db: AsyncSession, series_ids: Sequence[int]) -> None:
    """Read-only preflight, including every target before a bulk request starts."""
    linked = (
        select(IssueStoryArc.id)
        .join(Issue, Issue.id == IssueStoryArc.issue_id)
        .where(Issue.series_id.in_(series_ids))
    )
    files = (
        select(LibraryFile.id)
        .join(Issue, Issue.id == LibraryFile.issue_id)
        .where(Issue.series_id.in_(series_ids))
    )
    p = StoryArcPlacement
    imported = or_(p.creating_action_id.is_not(None), p.source_import_job_id.is_not(None))
    pending = or_(
        p.operation_token.is_not(None), p.last_result["trash_receipt"].as_string().is_not(None)
    )
    placement = (
        select(
            StoryArc.name,
            case(
                (
                    imported,
                    "This placement is retained for import rollback. Review its import history.",
                ),
                (pending, "Finish the interrupted arc file operation before retrying."),
                else_="Review and remove its managed arc copy or link before retrying.",
            ).label("reason"),
        )
        .select_from(p)
        .join(IssueStoryArc, IssueStoryArc.id == p.issue_story_arc_id)
        .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
        .where(
            or_(p.issue_story_arc_id.in_(linked), p.library_file_id.in_(files)),
            or_(imported, pending, p.ownership == StoryArcPlacementOwnership.MANAGED),
        )
        .order_by(StoryArc.id, p.id)
        .limit(1)
    )
    blocker = (await db.execute(placement)).first()
    if blocker is None:
        w = StoryArcSyncWork
        retained = or_(
            w.origin_import_action_id.is_not(None),
            w.origin_import_job_id.is_not(None),
            w.origin_imported_story_arc_id.is_not(None),
            w.origin_imported_story_arc_entry_id.is_not(None),
        )
        work = (
            select(
                StoryArc.name,
                case(
                    (
                        retained,
                        "This work is retained for import rollback. Review its import history.",
                    ),
                    else_="Finish or recover arc file synchronization before deleting this series.",
                ).label("reason"),
            )
            .select_from(w)
            .join(IssueStoryArc, IssueStoryArc.id == w.issue_story_arc_id)
            .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
            .where(
                or_(w.issue_story_arc_id.in_(linked), w.library_file_id.in_(files)),
                or_(
                    retained,
                    w.state.in_(
                        (
                            StoryArcSyncWorkState.QUEUED,
                            StoryArcSyncWorkState.RUNNING,
                            StoryArcSyncWorkState.RETRY_WAIT,
                        )
                    ),
                ),
            )
            .order_by(StoryArc.id, w.id)
            .limit(1)
        )
        blocker = (await db.execute(work)).first()
    if blocker is not None:
        raise ValidationError(
            f'Story Arc "{blocker.name}" still uses this series. {blocker.reason}'
        )


async def reserve_series_arc_work(db: AsyncSession, series_ids: Sequence[int]) -> None:
    """Recheck under locks before the caller removes reviewed records or files."""
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(update(Series).where(false()).values(path=Series.path))
    else:
        # PostgreSQL FK insertions take KEY SHARE locks. Freeze the parents as
        # well as existing children; otherwise a new placement could slip in.
        issues = select(Issue.id).where(Issue.series_id.in_(series_ids))
        files = select(LibraryFile.id).where(LibraryFile.issue_id.in_(issues))
        members = select(IssueStoryArc.id).where(IssueStoryArc.issue_id.in_(issues))
        for ids in (
            select(Series.id).where(Series.id.in_(series_ids)),
            issues,
            files,
            members,
            select(StoryArcPlacement.id).where(
                or_(
                    StoryArcPlacement.library_file_id.in_(files),
                    StoryArcPlacement.issue_story_arc_id.in_(members),
                )
            ),
            select(StoryArcSyncWork.id).where(
                or_(
                    StoryArcSyncWork.library_file_id.in_(files),
                    StoryArcSyncWork.issue_story_arc_id.in_(members),
                )
            ),
        ):
            locked = ids.order_by(*ids.selected_columns).with_for_update().subquery()
            await db.scalar(select(func.count()).select_from(locked))
    await check_series_arc_work(db, series_ids)


async def detach_series_arc_entries(db: AsyncSession, series: Series) -> None:
    """Detach exactly this series in the caller's reserved deletion transaction."""
    await reserve_series_arc_work(db, [series.id])
    linked = (
        await db.execute(
            select(IssueStoryArc, Issue.title, Issue.issue_number_text, Issue.issue_number)
            .join(Issue, Issue.id == IssueStoryArc.issue_id)
            .where(Issue.series_id == series.id)
            .order_by(IssueStoryArc.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not linked:
        return
    affected = set()
    for entry, title, number, legacy_number in linked:
        retain_missing_chapter(
            entry,
            series=series.title,
            title=title,
            number=number or format_issue_number(legacy_number),
        )
        affected.add(entry.story_arc_id)
    await db.flush()
    await db.execute(
        update(StoryArc).where(StoryArc.id.in_(affected)).values(revision=StoryArc.revision + 1)
    )
