"""Reading-order review never changes a member's exact canonical identity."""

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from pullbox.models.story_arc import IssueStoryArc, StoryArcResolutionState, StoryArcSourceKind


def retain_missing_chapter(
    member: IssueStoryArc, *, number: str | None, title: str | None, series: str | None
) -> None:
    """Retain display evidence without replacing the provider's original labels or user skip."""
    member.source_series_name = member.source_series_name or series
    member.source_issue_title = member.source_issue_title or title
    member.source_issue_number_text = member.source_issue_number_text or number
    member.issue = None
    member.sync_eligible = False
    if member.resolution_state != StoryArcResolutionState.SKIPPED:
        member.resolution_state = StoryArcResolutionState.MISSING


def provider_issue_identity(member: IssueStoryArc) -> str | None:
    """Return the exact provider identity, without interpreting import-local IDs."""
    if (
        member.source_kind == StoryArcSourceKind.PROVIDER
        or (member.evidence or {}).get("provider") == "comicvine"
    ):
        return member.source_issue_id
    return None


def requires_order_review(member: IssueStoryArc) -> bool:
    return (member.evidence or {}).get("catalog_review_required") is True


def order_review_filter() -> ColumnElement[bool]:
    """Portable SQLite/PostgreSQL JSON boolean with an absent-key default."""
    return func.coalesce(IssueStoryArc.evidence["catalog_review_required"].as_boolean(), False)
