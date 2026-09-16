"""Bounded presentation queries for Step 3 story-arc entries."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArcResolutionState, StoryArcSourceKind
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class StoryArcEntryResolutionFilter(enum.StrEnum):
    """Typed UI filter for all or one staged-entry resolution state."""

    ALL = "all"
    PENDING = StoryArcResolutionState.PENDING.value
    RESOLVED = StoryArcResolutionState.RESOLVED.value
    MISSING = StoryArcResolutionState.MISSING.value
    AMBIGUOUS = StoryArcResolutionState.AMBIGUOUS.value
    CONFLICT = StoryArcResolutionState.CONFLICT.value
    SKIPPED = StoryArcResolutionState.SKIPPED.value

    @property
    def resolution_state(self) -> StoryArcResolutionState | None:
        """Return the database state represented by this UI filter."""
        if self is StoryArcEntryResolutionFilter.ALL:
            return None
        return StoryArcResolutionState(self.value)


@dataclass(frozen=True, slots=True)
class ImportedStoryArcEntryReviewRow:
    """Safe presentation evidence for one staged story-arc entry."""

    id: int
    source_ordinal: int
    reading_order: int | None
    reading_order_text: str | None
    source_kind: StoryArcSourceKind
    source_series_name: str | None
    source_issue_number_text: str | None
    source_issue_title: str | None
    resolution_state: StoryArcResolutionState
    resolution_method: str | None
    resolution_confidence: float | None
    matched_issue_id: int | None
    matched_series_title: str | None
    matched_issue_number_text: str | None
    matched_issue_title: str | None
    source_location_present: bool
    selected_for_import: bool


@dataclass(frozen=True, slots=True)
class ImportedStoryArcEntryReviewPage:
    """One independently paginated entry page for a visible staged arc."""

    items: tuple[ImportedStoryArcEntryReviewRow, ...]
    total: int
    page: int
    page_size: int


async def load_import_story_arc_entry_review_page(
    session: AsyncSession,
    *,
    job_id: int,
    imported_story_arc_id: int,
    resolution_state: StoryArcResolutionState | None,
    page: int = 1,
    page_size: int = 25,
) -> ImportedStoryArcEntryReviewPage:
    """Load one entry page with a constant count-and-page query shape."""
    if page < 1:
        raise ValidationError("Story arc entry page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise ValidationError("Story arc entry page_size must be between 1 and 100")

    filters = [
        ImportedStoryArc.import_job_id == job_id,
        ImportedStoryArcEntry.imported_story_arc_id == imported_story_arc_id,
    ]
    if resolution_state is not None:
        filters.append(ImportedStoryArcEntry.resolution_state == resolution_state)

    total = int(
        await session.scalar(
            select(func.count(ImportedStoryArcEntry.id))
            .join(
                ImportedStoryArc,
                ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
            )
            .where(*filters)
        )
        or 0
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    bounded_page = min(page, total_pages)

    result = await session.execute(
        select(ImportedStoryArcEntry, Issue, Series)
        .join(
            ImportedStoryArc,
            ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
        )
        .outerjoin(Issue, Issue.id == ImportedStoryArcEntry.matched_issue_id)
        .outerjoin(Series, Series.id == Issue.series_id)
        .where(*filters)
        .order_by(
            ImportedStoryArcEntry.source_ordinal.asc(),
            ImportedStoryArcEntry.id.asc(),
        )
        .offset((bounded_page - 1) * page_size)
        .limit(page_size)
    )

    rows: list[ImportedStoryArcEntryReviewRow] = []
    for entry, issue, series in result.all():
        matched_issue_number_text: str | None = None
        if issue is not None:
            matched_issue_number_text = issue.issue_number_text or format_issue_number(
                issue.issue_number
            )
        rows.append(
            ImportedStoryArcEntryReviewRow(
                id=int(entry.id),
                source_ordinal=int(entry.source_ordinal),
                reading_order=entry.reading_order,
                reading_order_text=entry.reading_order_raw,
                source_kind=entry.source_kind,
                source_series_name=entry.source_series_name,
                source_issue_number_text=entry.source_issue_number_text,
                source_issue_title=entry.source_issue_title,
                resolution_state=entry.resolution_state,
                resolution_method=entry.resolution_method,
                resolution_confidence=entry.resolution_confidence,
                matched_issue_id=entry.matched_issue_id,
                matched_series_title=series.title if series is not None else None,
                matched_issue_number_text=matched_issue_number_text,
                matched_issue_title=issue.title if issue is not None else None,
                source_location_present=bool(
                    entry.source_location and entry.source_location.strip()
                ),
                selected_for_import=bool(entry.selected_for_import),
            )
        )

    return ImportedStoryArcEntryReviewPage(
        items=tuple(rows),
        total=total,
        page=bounded_page,
        page_size=page_size,
    )
