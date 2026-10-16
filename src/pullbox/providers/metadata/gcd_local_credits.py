"""Bounded, read-only structured GCD credits; free-text names are not identity proof."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import column, func, literal, not_, select, table

from pullbox.providers.metadata.gcd_local_database import (
    GcdDatabaseError,
    optional_profile,
    rows,
)
from pullbox.schemas.metadata_credits import MetadataCredit, parse_credits
from pullbox.schemas.metadata_sources import SourceStatus
from pullbox.services.metadata_source_reads import PAGE_SIZE

if TYPE_CHECKING:
    import sqlite3

    from sqlalchemy.sql import ColumnElement, Select
    from sqlalchemy.sql.selectable import TableClause

STORY = table("gcd_story", *map(column, ("id", "issue_id", "type_id", "deleted")))
NAME = table("gcd_creator_name_detail", *map(column, ("id", "name", "deleted")))
ROLE = table("gcd_credit_type", *map(column, ("id", "name")))
CREDIT_COLUMNS = ("id", "creator_id", "credit_type_id", "credit_name", "uncertain", "deleted")
STORY_CREDIT = table("gcd_story_credit", *map(column, (*CREDIT_COLUMNS, "story_id")))
ISSUE_CREDIT = table("gcd_issue_credit", *map(column, (*CREDIT_COLUMNS, "issue_id")))
PROFILE = (STORY, NAME, ROLE, STORY_CREDIT, ISSUE_CREDIT)
MAX_ROWS = PAGE_SIZE * 128
# GCD's CORE_TYPES: cartoon, cover/reprint, photo story, comic story, text story.
CORE_STORY_TYPES = (5, 6, 7, 13, 19, 21)

# Match documented credit names, not foreign IDs. Custom work labels stay descriptive.
ROLE_WORDS = {
    "script": "writer",
    "pencils": "penciller",
    "inks": "inker",
    "colors": "colorist",
    "letters": "letterer",
    "editing": "editor",
}
ART_ROLES = frozenset({"penciller", "inker", "colorist", "painting"})


def _role(value: str, custom: str, *, cover: bool) -> str:
    text = custom.strip() or value.strip()
    tokens = text.casefold().replace(", and ", ", ").replace(" and ", ", ").split(",")
    mapped = [ROLE_WORDS.get(token.strip(), token.strip()) for token in tokens]
    if cover:
        mapped = ["cover" if token in ART_ROLES else token for token in mapped]
    return ", ".join(dict.fromkeys(mapped))


def credit_query(credit: TableClause, identifiers: list[int]) -> Select[Any]:
    joined = credit.outerjoin(NAME, NAME.c.id == credit.c.creator_id).outerjoin(
        ROLE, ROLE.c.id == credit.c.credit_type_id
    )
    cover: ColumnElement[bool]
    if credit is STORY_CREDIT:
        joined = joined.join(STORY, STORY.c.id == credit.c.story_id)
        parent = STORY.c.issue_id
        cover = STORY.c.type_id.in_((6, 7))
        # Official dumps lack statistics: avoid scanning millions of core stories
        # through the low-selectivity type index instead of the requested issue IDs.
        conditions = [
            not_(STORY.c.deleted),
            func.coalesce(STORY.c.type_id, -1).in_(CORE_STORY_TYPES),
        ]
    else:
        parent = credit.c.issue_id
        cover = literal(False)
        conditions = []
    return (
        select(
            parent.label("issue_id"),
            func.substr(NAME.c.name, 1, 256).label("name"),
            NAME.c.deleted.label("name_deleted"),
            func.substr(ROLE.c.name, 1, 101).label("role"),
            func.substr(credit.c.credit_name, 1, 101).label("credit_name"),
            credit.c.uncertain,
            cover.label("cover"),
        )
        .select_from(joined)
        .where(parent.in_(identifiers), not_(credit.c.deleted), *conditions)
        .limit(MAX_ROWS + 1)
    )


def read_credits(
    db: sqlite3.Connection, identifiers: list[int]
) -> dict[int, tuple[MetadataCredit, ...] | None]:
    if not identifiers or not optional_profile(db, PROFILE):
        return {}
    records = rows(db, credit_query(STORY_CREDIT, identifiers)) + rows(
        db, credit_query(ISSUE_CREDIT, identifiers)
    )
    if len(records) > MAX_ROWS:
        raise GcdDatabaseError(
            "GCD credits exceed the bounded read limit.", SourceStatus.INCOMPATIBLE_RESPONSE
        )
    grouped: dict[int, set[tuple[str, str]]] = {}
    incomplete: set[int] = set()
    for row in records:
        identifier = row["issue_id"]
        if row["uncertain"] or row["name_deleted"] or row["name"] is None or row["role"] is None:
            incomplete.add(identifier)
            continue
        try:
            role = row["role"]
            custom = row["credit_name"] if row["credit_name"] is not None else ""
            if (
                not isinstance(role, str)
                or not isinstance(custom, str)
                or len(role) > 100
                or len(custom) > 100
            ):
                raise ValueError("Invalid GCD role")
            credit = MetadataCredit(
                name=row["name"],
                role=_role(role, custom, cover=bool(row["cover"])),
            )
        except ValueError as exc:
            raise GcdDatabaseError(
                "GCD supplied incompatible creator credits.", SourceStatus.INCOMPATIBLE_RESPONSE
            ) from exc
        grouped.setdefault(identifier, set()).add((credit.name, credit.role))
    try:
        result: dict[int, tuple[MetadataCredit, ...] | None] = {
            identifier: parse_credits(
                tuple({"name": name, "role": role} for name, role in sorted(credits))
            )
            for identifier, credits in grouped.items()
            if identifier not in incomplete
        }
    except ValueError as exc:
        raise GcdDatabaseError(
            "GCD supplied incompatible creator credits.", SourceStatus.INCOMPATIBLE_RESPONSE
        ) from exc
    result.update(dict.fromkeys(incomplete))
    return result
