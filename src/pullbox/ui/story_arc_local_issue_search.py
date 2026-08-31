"""Bounded local-library issue search for unresolved Story Arc entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, exists, func, or_, select

from pullbox.core.db_utils import escape_like
from pullbox.core.issue_numbers import format_issue_number, parse_issue_number_text
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


LOCAL_ISSUE_RESULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class StoryArcLocalIssueCandidate:
    """One path-free canonical issue candidate safe to render in a picker."""

    issue_id: int
    series_title: str
    issue_number_text: str
    issue_title: str
    status: str
    canonical_file_available: bool


@dataclass(frozen=True, slots=True)
class StoryArcLocalIssueSearchResult:
    """A fixed-size result page; local search intentionally has no full export."""

    query: str
    items: tuple[StoryArcLocalIssueCandidate, ...]
    has_more: bool
    limit: int = LOCAL_ISSUE_RESULT_LIMIT


async def search_story_arc_local_issues(
    session: AsyncSession,
    *,
    query: str,
    source_series_name: str | None,
    source_issue_number_text: str | None,
) -> StoryArcLocalIssueSearchResult:
    """Search canonical issues without providers or unbounded relationship loads."""
    search_text = query.strip()
    if not search_text:
        return StoryArcLocalIssueSearchResult(query="", items=(), has_more=False)

    escaped = escape_like(search_text)
    pattern = f"%{escaped}%"
    normalized = search_text.casefold()
    exact_source_series = (source_series_name or "").strip().casefold()
    exact_source_number = (source_issue_number_text or "").strip().casefold()

    issue_number_filters: list[ColumnElement[bool]] = []
    try:
        numeric_query, exact_query = parse_issue_number_text(search_text)
    except ValueError:
        issue_number_filters.append(Issue.issue_number_text.ilike(pattern, escape="\\"))
    else:
        issue_number_filters.append(func.lower(Issue.issue_number_text) == exact_query.casefold())
        if exact_query == format_issue_number(numeric_query):
            issue_number_filters.append(
                and_(
                    Issue.issue_number_text.is_(None),
                    Issue.issue_number == numeric_query,
                )
            )
    issue_number_match = or_(*issue_number_filters)
    filters = or_(
        Series.title.ilike(pattern, escape="\\"),
        Issue.title.ilike(pattern, escape="\\"),
        issue_number_match,
    )
    has_file = exists(select(LibraryFile.id).where(LibraryFile.issue_id == Issue.id).limit(1))
    rows = (
        await session.execute(
            select(
                Issue.id,
                Series.title,
                Issue.issue_number_text,
                Issue.issue_number,
                Issue.title,
                Issue.status,
                has_file.label("canonical_file_available"),
            )
            .join(Series, Series.id == Issue.series_id)
            .where(filters)
            .order_by(
                case(
                    (
                        func.lower(Series.title) == exact_source_series,
                        0,
                    ),
                    else_=1,
                ),
                case(
                    (
                        func.lower(Issue.issue_number_text) == exact_source_number,
                        0,
                    ),
                    else_=1,
                ),
                case((func.lower(Series.title) == normalized, 0), else_=1),
                case((func.lower(Issue.issue_number_text) == normalized, 0), else_=1),
                Series.sort_title.asc(),
                Issue.issue_number.asc(),
                Issue.issue_number_text.asc(),
                Issue.id.asc(),
            )
            .limit(LOCAL_ISSUE_RESULT_LIMIT + 1)
        )
    ).all()
    items = tuple(
        StoryArcLocalIssueCandidate(
            issue_id=int(row.id),
            series_title=str(row.title),
            issue_number_text=(
                str(row.issue_number_text)
                if row.issue_number_text is not None
                else format_issue_number(float(row.issue_number))
            ),
            issue_title=str(row[4] or "Untitled issue"),
            status=row.status.value,
            canonical_file_available=bool(row.canonical_file_available),
        )
        for row in rows[:LOCAL_ISSUE_RESULT_LIMIT]
    )
    return StoryArcLocalIssueSearchResult(
        query=search_text,
        items=items,
        has_more=len(rows) > LOCAL_ISSUE_RESULT_LIMIT,
    )
