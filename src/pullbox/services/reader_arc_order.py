"""Resolve the current chapter and its immediate neighbors in saved arc order."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.page_sources import SUPPORTED_READER_FORMATS
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState


@dataclass(frozen=True, slots=True)
class ArcNeighbor:
    entry_id: int
    issue_id: int | None
    label: str
    title: str | None
    gap: str | None


@dataclass(frozen=True, slots=True)
class ArcReaderOrder:
    name: str
    entry_id: int
    position: int
    total: int
    previous: ArcNeighbor | None
    next: ArcNeighbor | None


async def list_reader_arcs(session: AsyncSession, issue_id: int) -> Sequence[tuple[int, int, str]]:
    """Return only confirmed memberships for this issue, including archived arcs."""
    result = await session.execute(
        select(StoryArc.id, IssueStoryArc.id, StoryArc.name)
        .join(IssueStoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
        .where(
            IssueStoryArc.issue_id == issue_id,
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
        .order_by(StoryArc.normalized_name, StoryArc.id)
    )
    return result.tuples().all()


async def load_arc_reader_order(
    session: AsyncSession, arc_id: int, issue_id: int, entry_id: int | None
) -> ArcReaderOrder | None:
    member = IssueStoryArc
    order = (member.sequence_number, member.source_ordinal, member.id)
    # Rank all memberships (including missing chapters), not just the current table page.
    ranked = (
        select(
            member.id,
            member.issue_id,
            member.resolution_state,
            func.row_number().over(order_by=order).label("position"),
            func.count().over().label("total"),
            func.lag(member.id).over(order_by=order).label("prev_id"),
            func.lead(member.id).over(order_by=order).label("next_id"),
        )
        .where(member.story_arc_id == arc_id)
        .subquery()
    )
    anchor = (
        select(ranked, StoryArc.name)
        .join_from(ranked, StoryArc, StoryArc.id == arc_id)
        .where(
            ranked.c.issue_id == issue_id,
            ranked.c.resolution_state == StoryArcResolutionState.RESOLVED,
        )
    )
    if entry_id is not None:
        anchor = anchor.where(ranked.c.id == entry_id)
    current = (await session.execute(anchor)).one_or_none()
    if current is None:
        return None
    neighbors: dict[int, ArcNeighbor] = {}
    ids = [key for key in (current.prev_id, current.next_id) if key is not None]
    if ids:
        has_file = (
            select(LibraryFile.id)
            .where(
                LibraryFile.issue_id == Issue.id,
                LibraryFile.file_format.in_(SUPPORTED_READER_FORMATS),
            )
            .exists()
        )
        rows = await session.execute(
            select(
                member.id,
                member.issue_id,
                member.resolution_state,
                member.source_issue_title,
                member.source_series_name,
                member.source_issue_number_text,
                Issue.issue_number,
                Issue.issue_number_text,
                Issue.title,
                Issue.status,
                Series.title.label("series_title"),
                has_file.label("has_file"),
            )
            .outerjoin(Issue, Issue.id == member.issue_id)
            .outerjoin(Series, Series.id == Issue.series_id)
            .where(member.story_arc_id == arc_id, member.id.in_(ids))
        )
        for row in rows:
            matched = (
                row.resolution_state == StoryArcResolutionState.RESOLVED
                and row.issue_id is not None
            )
            if matched:
                number = row.issue_number_text or format_issue_number(row.issue_number)
                label = f"{row.series_title} #{number}"
                gap = (
                    None
                    if row.status == IssueStatus.OWNED and row.has_file
                    else f"{label} has no readable library file."
                )
            else:
                label = (
                    row.source_issue_title
                    or " ".join(
                        filter(
                            None,
                            (
                                row.source_series_name,
                                f"#{row.source_issue_number_text}"
                                if row.source_issue_number_text
                                else None,
                            ),
                        )
                    )
                    or "This entry"
                )
                gap = f"{label} has no confirmed issue match."
            neighbors[row.id] = ArcNeighbor(row.id, row.issue_id, label, row.title, gap)
        if len(neighbors) != len(ids):
            return None
    return ArcReaderOrder(
        current.name,
        current.id,
        current.position,
        current.total,
        neighbors.get(current.prev_id),
        neighbors.get(current.next_id),
    )
