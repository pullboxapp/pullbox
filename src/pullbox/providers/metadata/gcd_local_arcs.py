"""Native, bounded arc reads from the official dump's story associations."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, column, func, not_, or_, select, table

from pullbox.core.metadata_identity import MetadataSource
from pullbox.providers.metadata.gcd_local_database import (
    ISSUE,
    SERIES,
    GcdDatabaseError,
    GcdSnapshot,
    optional_profile,
    rows,
)
from pullbox.schemas.metadata_sources import MetadataPage, ProviderStoryArcRead, SourceStatus

if TYPE_CHECKING:
    import sqlite3

    from sqlalchemy.sql import Select

ARC = table(
    "gcd_story_arc",
    *map(
        column,
        (
            "id",
            "name",
            "sort_name",
            "disambiguation",
            "description",
            "notes",
            "language_id",
            "deleted",
        ),
    ),
)
STORY = table("gcd_story", *map(column, ("id", "issue_id", "deleted")))
MEMBER = table("gcd_story_story_arc", *map(column, ("id", "story_id", "storyarc_id")))
BASE = ISSUE.alias("arc_base_issue")
MEMBER_FROM = (
    MEMBER.outerjoin(STORY, STORY.c.id == MEMBER.c.story_id)
    .outerjoin(ISSUE, ISSUE.c.id == STORY.c.issue_id)
    .outerjoin(SERIES, SERIES.c.id == ISSUE.c.series_id)
    .outerjoin(BASE, BASE.c.id == ISSUE.c.variant_of_id)
)
PUBLIC = (
    not_(STORY.c.deleted)
    & not_(ISSUE.c.deleted)
    & not_(SERIES.c.deleted)
    & (SERIES.c.is_comics_publication == 1)
)
MAX_MEMBERS = 5000
WARNINGS = [
    "GCD publication order is a starting point, not a curated reading order. Review it before Add.",
    "GCD story associations may include reprints. Review which issues belong in your arc.",
]


def require_arcs(db: sqlite3.Connection) -> None:
    if not optional_profile(db, (ARC, STORY, MEMBER)):
        raise GcdDatabaseError(
            "This dump does not contain the GCD Story Arc tables.", SourceStatus.UNSUPPORTED
        )


def arc_query() -> Select[Any]:
    return select(
        ARC.c.id,
        func.substr(ARC.c.name, 1, 501).label("name"),
        func.substr(ARC.c.disambiguation, 1, 501).label("disambiguation"),
        func.substr(ARC.c.description, 1, 20000).label("description"),
        func.substr(ARC.c.notes, 1, 20000).label("notes"),
    ).where(not_(ARC.c.deleted))


def members_query(identifier: str) -> Select[Any]:
    # Start at the indexed arc membership, not a scan of millions of issues/stories.
    # Multiple stories in one issue are one membership, never duplicate issues.
    return (
        select(ISSUE)
        .select_from(MEMBER_FROM)
        .where(MEMBER.c.storyarc_id == identifier, PUBLIC)
        .group_by(ISSUE.c.id)
        .order_by(
            ISSUE.c.key_date,
            ISSUE.c.on_sale_date,
            SERIES.c.sort_name,
            ISSUE.c.sort_code,
            ISSUE.c.id,
        )
    )


def validate_members(db: sqlite3.Connection, identifier: str) -> None:
    invalid = or_(
        STORY.c.id.is_(None),
        not_(STORY.c.deleted)
        & or_(
            ISSUE.c.id.is_(None),
            SERIES.c.id.is_(None),
            PUBLIC
            & ISSUE.c.variant_of_id.is_not(None)
            & or_(BASE.c.id.is_(None), BASE.c.series_id == ISSUE.c.series_id),
        ),
    )
    if rows(
        db,
        select(MEMBER.c.id)
        .select_from(MEMBER_FROM)
        .where(MEMBER.c.storyarc_id == identifier, invalid)
        .limit(1),
    ):
        raise GcdDatabaseError(
            "GCD arc membership contains an unresolved variant or broken identity.",
            SourceStatus.INCOMPATIBLE_RESPONSE,
        )


def arc_row(
    row: sqlite3.Row, snapshot: GcdSnapshot, ids: list[str] | None = None
) -> ProviderStoryArcRead:
    title = row["name"] + (f" [{row['disambiguation']}]" if row["disambiguation"] else "")
    if not title.strip() or len(title) > 500:
        raise GcdDatabaseError(
            "The GCD arc title is incompatible.", SourceStatus.INCOMPATIBLE_RESPONSE
        )
    description = "\n\n".join(text for text in (row["description"], row["notes"]) if text)
    return ProviderStoryArcRead(
        source=MetadataSource.GCD_LOCAL,
        identity_namespace=MetadataSource.GCD_LOCAL.identity_namespace,
        external_id=str(row["id"]),
        title=title,
        description=html.escape(description[:20000]) or None,
        resource_url=f"https://www.comics.org/story_arc/{row['id']}/",
        source_updated_at=snapshot.validated_at,
        declared_issue_count=len(ids) if ids is not None else None,
        issue_external_ids=ids,
        membership_complete=ids is not None,
        warnings=list(WARNINGS),
    )


def find_arc(db: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    require_arcs(db)
    found = rows(db, arc_query().where(ARC.c.id == identifier))
    return found[0] if found else None


def read_arc(
    db: sqlite3.Connection, snapshot: GcdSnapshot, identifier: str
) -> ProviderStoryArcRead | None:
    found = find_arc(db, identifier)
    if found is None:
        return None
    validate_members(db, identifier)
    members = rows(
        db, members_query(identifier).with_only_columns(ISSUE.c.id).limit(MAX_MEMBERS + 1)
    )
    if len(members) > MAX_MEMBERS:
        raise GcdDatabaseError(
            "The GCD arc exceeds the membership limit.", SourceStatus.INCOMPATIBLE_RESPONSE
        )
    return arc_row(found, snapshot, [str(member["id"]) for member in members])


def read_members(
    db: sqlite3.Connection, identifier: str, page: int
) -> tuple[list[sqlite3.Row], int] | None:
    if find_arc(db, identifier) is None:
        return None
    validate_members(db, identifier)
    query = members_query(identifier)
    total = rows(db, select(func.count()).select_from(query.order_by(None).subquery()))[0][0]
    if total > MAX_MEMBERS:
        raise GcdDatabaseError(
            "The GCD arc exceeds the membership limit.", SourceStatus.INCOMPATIBLE_RESPONSE
        )
    return rows(db, query.limit(100).offset((page - 1) * 100)), total


def search_arcs(
    db: sqlite3.Connection, snapshot: GcdSnapshot, query: str, page: int
) -> MetadataPage[ProviderStoryArcRead]:
    require_arcs(db)
    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:16]
    if not terms:
        return MetadataPage(results=[], total=0)
    conditions = [
        not_(ARC.c.deleted),
        *(ARC.c.name.icontains(term, autoescape=True) for term in terms),
    ]
    total = rows(db, select(func.count()).select_from(ARC).where(*conditions))[0][0]
    found = rows(
        db,
        arc_query()
        .where(*conditions)
        .order_by(
            case((func.lower(ARC.c.name) == query.lower(), 0), else_=1),
            ARC.c.sort_name,
            ARC.c.disambiguation,
            ARC.c.language_id,
            ARC.c.id,
        )
        .limit(100)
        .offset((page - 1) * 100),
    )
    more = page * 100 < total
    return MetadataPage(
        results=[arc_row(row, snapshot) for row in found],
        total=total,
        next_page=page + 1 if more and page < 100 else None,
        truncated=more and page == 100,
    )
