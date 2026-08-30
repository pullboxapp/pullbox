"""Bounded query presenters for normal Story Arc management pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select
from sqlalchemy.orm import joinedload

from pullbox.core.db_utils import escape_like
from pullbox.models.issue import Issue
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import Subquery


@dataclass(frozen=True, slots=True)
class StoryArcListItemView:
    """One compact Story Arc row with aggregate entry state."""

    id: int
    name: str
    description: str | None
    lifecycle: str
    source_kind: str
    monitored: bool
    sync_enabled: bool
    revision: int
    membership_count: int
    resolved_count: int
    missing_count: int
    conflict_count: int
    completion_label: str


@dataclass(frozen=True, slots=True)
class StoryArcListPageView:
    """A bounded Story Arc registry page."""

    items: tuple[StoryArcListItemView, ...]
    total: int
    page: int
    per_page: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class StoryArcMembershipView:
    """One ordered membership with fully loaded canonical context."""

    id: int
    issue_id: int | None
    position: int
    sequence_number: int
    source_ordinal: int
    exact_issue_number: str
    series_name: str
    issue_title: str
    resolution_state: str
    resolution_label: str
    canonical_state: str
    canonical_file_available: bool
    can_resolve: bool
    is_first: bool
    is_last: bool


@dataclass(frozen=True, slots=True)
class StoryArcDetailView:
    """Story Arc metadata and one bounded ordered membership page."""

    id: int
    name: str
    description: str | None
    lifecycle: str
    source_kind: str
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    sync_enabled: bool
    revision: int
    membership_count: int
    resolved_count: int
    missing_count: int
    conflict_count: int
    memberships: tuple[StoryArcMembershipView, ...]
    page: int
    per_page: int
    total_pages: int
    next_sequence_number: int

    @property
    def active(self) -> bool:
        """Whether membership and automation controls remain editable."""
        return self.lifecycle == StoryArcLifecycle.ACTIVE.value


def _membership_counts_subquery() -> Subquery:
    """Aggregate membership state once for a bounded registry query."""
    return (
        select(
            IssueStoryArc.story_arc_id.label("story_arc_id"),
            func.count(IssueStoryArc.id).label("membership_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED, 1),
                    else_=0,
                )
            ).label("resolved_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.MISSING, 1),
                    else_=0,
                )
            ).label("missing_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.CONFLICT, 1),
                    else_=0,
                )
            ).label("conflict_count"),
        )
        .group_by(IssueStoryArc.story_arc_id)
        .subquery()
    )


def _completion_label(*, total: int, resolved: int) -> str:
    if total == 0:
        return "Empty"
    return f"{resolved} of {total} resolved"


async def load_story_arc_list_page(
    session: AsyncSession,
    *,
    q: str | None,
    lifecycle: StoryArcLifecycle | None,
    monitored: bool | None,
    page: int,
    per_page: int,
) -> StoryArcListPageView:
    """Load one stable Story Arc registry page with literal search semantics."""
    filters: list[ColumnElement[bool]] = []
    if q is not None and (search_text := q.strip()):
        filters.append(StoryArc.name.ilike(f"%{escape_like(search_text)}%", escape="\\"))
    if lifecycle is not None:
        filters.append(StoryArc.lifecycle == lifecycle)
    if monitored is not None:
        filters.append(StoryArc.monitored.is_(monitored))

    total = int(await session.scalar(select(func.count(StoryArc.id)).where(*filters)) or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = min(page, total_pages)
    counts = _membership_counts_subquery()
    rows = (
        await session.execute(
            select(
                StoryArc,
                func.coalesce(counts.c.membership_count, 0),
                func.coalesce(counts.c.resolved_count, 0),
                func.coalesce(counts.c.missing_count, 0),
                func.coalesce(counts.c.conflict_count, 0),
            )
            .outerjoin(counts, counts.c.story_arc_id == StoryArc.id)
            .where(*filters)
            .order_by(StoryArc.normalized_name.asc(), StoryArc.id.asc())
            .limit(per_page)
            .offset((safe_page - 1) * per_page)
        )
    ).all()
    items: list[StoryArcListItemView] = []
    for arc, membership_count, resolved_count, missing_count, conflict_count in rows:
        membership_total = int(membership_count)
        resolved_total = int(resolved_count)
        items.append(
            StoryArcListItemView(
                id=arc.id,
                name=arc.name,
                description=arc.description,
                lifecycle=arc.lifecycle.value,
                source_kind=arc.source_kind.value,
                monitored=arc.monitored,
                sync_enabled=arc.sync_enabled,
                revision=arc.revision,
                membership_count=membership_total,
                resolved_count=resolved_total,
                missing_count=int(missing_count),
                conflict_count=int(conflict_count),
                completion_label=_completion_label(
                    total=membership_total,
                    resolved=resolved_total,
                ),
            )
        )
    return StoryArcListPageView(
        items=tuple(items),
        total=total,
        page=safe_page,
        per_page=per_page,
        total_pages=total_pages,
    )


def _resolution_label(state: StoryArcResolutionState) -> str:
    return {
        StoryArcResolutionState.PENDING: "Pending review",
        StoryArcResolutionState.RESOLVED: "Resolved",
        StoryArcResolutionState.MISSING: "Missing",
        StoryArcResolutionState.AMBIGUOUS: "Ambiguous",
        StoryArcResolutionState.CONFLICT: "Conflict",
        StoryArcResolutionState.SKIPPED: "Skipped",
    }[state]


def _canonical_state(issue: Issue | None) -> str:
    if issue is None:
        return "Not matched"
    if issue.library_file is not None:
        return "File available"
    if issue.status.value == "owned":
        return "Owned record; file unavailable"
    return issue.status.value.replace("_", " ").title()


def _present_membership(
    membership: IssueStoryArc,
    *,
    position: int,
    membership_total: int,
) -> StoryArcMembershipView:
    issue = membership.issue
    exact_issue_number = membership.source_issue_number_text
    if exact_issue_number is None and issue is not None:
        exact_issue_number = issue.effective_issue_number_text
    exact_issue_number = exact_issue_number or "Unknown"
    series_name = membership.source_series_name
    if series_name is None and issue is not None:
        series_name = issue.series.title
    issue_title = membership.source_issue_title
    if issue_title is None and issue is not None:
        issue_title = issue.title
    return StoryArcMembershipView(
        id=membership.id,
        issue_id=membership.issue_id,
        position=position,
        sequence_number=membership.sequence_number,
        source_ordinal=membership.source_ordinal,
        exact_issue_number=exact_issue_number,
        series_name=series_name or "Series not matched",
        issue_title=issue_title or "Untitled issue",
        resolution_state=membership.resolution_state.value,
        resolution_label=_resolution_label(membership.resolution_state),
        canonical_state=_canonical_state(issue),
        canonical_file_available=issue is not None and issue.library_file is not None,
        can_resolve=(
            membership.issue_id is None
            or membership.resolution_state
            in {
                StoryArcResolutionState.PENDING,
                StoryArcResolutionState.MISSING,
                StoryArcResolutionState.AMBIGUOUS,
                StoryArcResolutionState.CONFLICT,
            }
        ),
        is_first=position == 1,
        is_last=position == membership_total,
    )


async def load_story_arc_detail(
    session: AsyncSession,
    *,
    story_arc_id: int,
    page: int,
    per_page: int,
) -> StoryArcDetailView | None:
    """Load one arc and one bounded, relationship-complete membership page."""
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        return None

    count_row = (
        await session.execute(
            select(
                func.count(IssueStoryArc.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.MISSING,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.CONFLICT,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(IssueStoryArc.story_arc_id == story_arc_id)
        )
    ).one()
    membership_total = int(count_row[0])
    total_pages = max(1, (membership_total + per_page - 1) // per_page)
    safe_page = min(page, total_pages)
    offset = (safe_page - 1) * per_page
    memberships = list(
        (
            await session.scalars(
                select(IssueStoryArc)
                .where(IssueStoryArc.story_arc_id == story_arc_id)
                .options(
                    joinedload(IssueStoryArc.issue).joinedload(Issue.series),
                    joinedload(IssueStoryArc.issue).joinedload(Issue.library_file),
                )
                .order_by(
                    IssueStoryArc.sequence_number.asc(),
                    IssueStoryArc.source_ordinal.asc(),
                    IssueStoryArc.id.asc(),
                )
                .limit(per_page)
                .offset(offset)
            )
        ).unique()
    )
    membership_views = tuple(
        _present_membership(
            membership,
            position=offset + index,
            membership_total=membership_total,
        )
        for index, membership in enumerate(memberships, start=1)
    )
    return StoryArcDetailView(
        id=arc.id,
        name=arc.name,
        description=arc.description,
        lifecycle=arc.lifecycle.value,
        source_kind=arc.source_kind.value,
        monitored=arc.monitored,
        search_missing=arc.search_missing,
        include_upcoming=arc.include_upcoming,
        sync_enabled=arc.sync_enabled,
        revision=arc.revision,
        membership_count=membership_total,
        resolved_count=int(count_row[1]),
        missing_count=int(count_row[2]),
        conflict_count=int(count_row[3]),
        memberships=membership_views,
        page=safe_page,
        per_page=per_page,
        total_pages=total_pages,
        next_sequence_number=membership_total + 1,
    )
