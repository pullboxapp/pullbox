"""Reading-order review never changes a member's exact canonical identity."""

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from pullbox.models.story_arc import IssueStoryArc


def requires_order_review(member: IssueStoryArc) -> bool:
    return (member.evidence or {}).get("catalog_review_required") is True


def order_review_filter() -> ColumnElement[bool]:
    """Portable SQLite/PostgreSQL JSON boolean with an absent-key default."""
    return func.coalesce(IssueStoryArc.evidence["catalog_review_required"].as_boolean(), False)
