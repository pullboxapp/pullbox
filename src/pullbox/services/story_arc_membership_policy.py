"""Reading-order review never changes a member's exact canonical identity."""

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from pullbox.models.story_arc import IssueStoryArc, StoryArcSourceKind


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
