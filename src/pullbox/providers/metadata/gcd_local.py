"""GCD local series discovery and exact catalog reads, without cover or crosswalk claims."""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, not_, or_, select

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.providers.metadata import gcd_local_arcs
from pullbox.providers.metadata.gcd_local_credits import read_credits
from pullbox.providers.metadata.gcd_local_database import (
    ISSUE,
    LANGUAGE,
    PUBLISHER,
    SERIES,
    GcdDatabaseError,
    GcdSnapshot,
    assert_current,
    disk_read,
    open_readonly,
    rows,
)
from pullbox.schemas.metadata_sources import (
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
    SeriesDiscoveryQuery,
    SourceStatus,
)
from pullbox.services.metadata_discovery import MetadataSourceError, SourcePage
from pullbox.services.metadata_source_reads import MAX_PAGE, PAGE_SIZE, page_number, source_id

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Callable

    from sqlalchemy.sql import Select

    from pullbox.schemas.metadata_credits import MetadataCredit

SOURCE = MetadataSource.GCD_LOCAL
BASE = ISSUE.alias("base_issue")
ISSUE_FROM = ISSUE.outerjoin(BASE, ISSUE.c.variant_of_id == BASE.c.id)
CANONICAL = or_(ISSUE.c.variant_of_id.is_(None), BASE.c.series_id != ISSUE.c.series_id)
# Official dumps lack planner statistics. Boolean NOT avoids the low-selectivity
# deleted index, letting these per-series reads use the existing series index.
ISSUE_PUBLIC = not_(ISSUE.c.deleted)
PUBLIC = (SERIES.c.deleted == 0) & (SERIES.c.is_comics_publication == 1)
SERIES_FROM = SERIES.outerjoin(
    PUBLISHER, (PUBLISHER.c.id == SERIES.c.publisher_id) & (PUBLISHER.c.deleted == 0)
).outerjoin(LANGUAGE, LANGUAGE.c.id == SERIES.c.language_id)


def issue_query() -> Select[Any]:
    return select(ISSUE).select_from(ISSUE_FROM).where(ISSUE_PUBLIC, CANONICAL)


def series_query() -> Select[Any]:
    count = (
        select(func.count())
        .select_from(ISSUE_FROM)
        .where(
            ISSUE.c.series_id == SERIES.c.id,
            ISSUE_PUBLIC,
            CANONICAL,
        )
        .correlate(SERIES)
        .scalar_subquery()
    )
    return (
        select(
            SERIES,
            PUBLISHER.c.name.label("publisher"),
            LANGUAGE.c.code.label("language"),
            count.label("issue_count"),
        )
        .select_from(SERIES_FROM)
        .where(PUBLIC)
    )


def _date(value: str | None) -> date | None:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass  # GCD legitimately stores partial dates, including YYYY-MM-00.
    return None


def _series(row: sqlite3.Row, snapshot: GcdSnapshot) -> ProviderSeriesRead:
    return ProviderSeriesRead(
        source=SOURCE,
        identity_namespace=SOURCE.identity_namespace,
        external_id=str(row["id"]),
        title=row["name"],
        sort_title=row["sort_name"],
        year_start=row["year_began"] or None,
        year_end=row["year_ended"] or None,
        publisher=row["publisher"],
        issue_count=row["issue_count"],
        language=row["language"],
        status="continuing" if row["is_current"] else "ended",
        description=html.escape(row["notes"][:20000]) if row["notes"] else None,
        resource_url=f"https://www.comics.org/series/{row['id']}/",
        source_updated_at=snapshot.validated_at,
    )


def _issue(
    row: sqlite3.Row,
    snapshot: GcdSnapshot,
    credits: tuple[MetadataCredit, ...] | None = None,
) -> ProviderIssueRead:
    number = row["number"]
    try:
        key = normalize_issue_number_text(number)
    except ValueError:
        key = None
    pages = row["page_count"]
    return ProviderIssueRead(
        credits=credits,
        source=SOURCE,
        identity_namespace=SOURCE.identity_namespace,
        external_id=str(row["id"]),
        series_external_id=str(row["series_id"]),
        issue_number_text=number,
        issue_number_key=key,
        title=row["title"] or None,
        description=html.escape(row["notes"][:20000]) if row["notes"] else None,
        cover_date=_date(row["key_date"]),
        store_date=_date(row["on_sale_date"]),
        page_count=int(pages) if pages is not None and pages >= 0 and int(pages) == pages else None,
        resource_url=f"https://www.comics.org/issue/{row['id']}/",
        source_updated_at=snapshot.validated_at,
    )


class GcdLocalSource:
    def __init__(self, snapshot: GcdSnapshot | None) -> None:
        if snapshot is None:
            raise MetadataSourceError(SourceStatus.UNCONFIGURED)
        self.snapshot = snapshot

    async def _read[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def read(stop: threading.Event, deadline: float) -> T:
            assert_current(self.snapshot)
            with open_readonly(Path(self.snapshot.path), stop, deadline) as db:
                result = operation(db)
            assert_current(self.snapshot)
            return result

        try:
            return await disk_read(read)
        except GcdDatabaseError as exc:
            raise MetadataSourceError(exc.status) from exc

    async def check(self) -> None:
        await self._read(lambda db: rows(db, series_query().limit(1)))

    async def cache_token(self) -> str:
        return await self._read(
            lambda db: self.snapshot.sha256 + ":" + self.snapshot.validated_at.isoformat()
        )

    async def close(self) -> None:
        return None

    async def story_arcs(self, query: str, *, page: int = 1) -> MetadataPage[ProviderStoryArcRead]:
        page_number(page)
        if page > 100:
            raise ValueError("Story arc search is bounded to 100 pages")
        return await self._read(
            lambda db: gcd_local_arcs.search_arcs(db, self.snapshot, query, page)
        )

    async def story_arc(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderStoryArcRead]:
        identifier = source_id(SOURCE, MetadataEntityKind.STORY_ARC, external_id)
        result = await self._read(lambda db: gcd_local_arcs.read_arc(db, self.snapshot, identifier))
        return MetadataFetch(
            status=SourceStatus.OK if result else SourceStatus.NOT_FOUND, data=result
        )

    async def story_arc_issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = source_id(SOURCE, MetadataEntityKind.STORY_ARC, external_id)
        page_number(page)
        if page > 50:
            raise ValueError("Story arc membership is bounded to 5000 issues")

        def read(db: sqlite3.Connection) -> MetadataPage[ProviderIssueRead] | None:
            result = gcd_local_arcs.read_members(db, identifier, page)
            if result is None:
                return None
            found, total = result
            credits = read_credits(db, [row["id"] for row in found])
            return MetadataPage(
                results=[_issue(row, self.snapshot, credits.get(row["id"])) for row in found],
                total=total,
                next_page=page + 1 if page * PAGE_SIZE < total else None,
            )

        result = await self._read(read)
        return MetadataFetch(
            status=SourceStatus.OK if result else SourceStatus.NOT_FOUND, data=result
        )

    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        terms = re.findall(r"\w+", query.query, flags=re.UNICODE)[:16]
        if not terms:
            return SourcePage([], 0)
        conditions = [SERIES.c.name.icontains(term, autoescape=True) for term in terms]
        if query.year is not None:
            conditions.append(SERIES.c.year_began == query.year)

        def read(db: sqlite3.Connection) -> SourcePage:
            total = rows(db, select(func.count()).select_from(SERIES).where(PUBLIC, *conditions))[
                0
            ][0]
            found = rows(
                db,
                series_query()
                .where(*conditions)
                .order_by(
                    case((func.lower(SERIES.c.name) == query.query.lower(), 0), else_=1),
                    SERIES.c.sort_name,
                    SERIES.c.year_began,
                    SERIES.c.id,
                )
                .limit(query.limit_per_source)
                .offset(offset),
            )
            more = offset + len(found) < total
            next_offset = (
                offset + query.limit_per_source
                if more and offset + query.limit_per_source <= 10000
                else None
            )
            return SourcePage(
                [_series(row, self.snapshot) for row in found],
                total,
                next_offset,
                truncated=more and next_offset is None,
            )

        return await self._read(read)

    async def series(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        identifier = int(source_id(SOURCE, MetadataEntityKind.SERIES, external_id))
        found = await self._read(
            lambda db: rows(db, series_query().where(SERIES.c.id == identifier))
        )
        return MetadataFetch(
            status=SourceStatus.OK if found else SourceStatus.NOT_FOUND,
            data=_series(found[0], self.snapshot) if found else None,
        )

    async def issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = int(source_id(SOURCE, MetadataEntityKind.SERIES, external_id))
        page_number(page)

        def read(db: sqlite3.Connection) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
            profile = rows(db, series_query().where(SERIES.c.id == identifier))
            if not profile:
                return MetadataFetch(status=SourceStatus.NOT_FOUND)
            total = profile[0]["issue_count"]
            found = rows(
                db,
                issue_query()
                .where(ISSUE.c.series_id == identifier)
                .order_by(ISSUE.c.sort_code, ISSUE.c.id)
                .limit(PAGE_SIZE)
                .offset((page - 1) * PAGE_SIZE),
            )
            more = page * PAGE_SIZE < total
            credits = read_credits(db, [row["id"] for row in found])
            return MetadataFetch(
                status=SourceStatus.OK,
                data=MetadataPage(
                    results=[_issue(row, self.snapshot, credits.get(row["id"])) for row in found],
                    total=total,
                    next_page=page + 1 if more and page < MAX_PAGE else None,
                    truncated=more and page == MAX_PAGE,
                ),
            )

        return await self._read(read)

    async def issue(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        identifier = int(source_id(SOURCE, MetadataEntityKind.ISSUE, external_id))

        def read(db: sqlite3.Connection) -> ProviderIssueRead | None:
            found = rows(
                db,
                issue_query()
                .join(SERIES, SERIES.c.id == ISSUE.c.series_id)
                .where(ISSUE.c.id == identifier, PUBLIC),
            )
            if not found:
                return None
            credits = read_credits(db, [identifier])
            return _issue(found[0], self.snapshot, credits.get(identifier))

        result = await self._read(read)
        return MetadataFetch(
            status=SourceStatus.OK if result else SourceStatus.NOT_FOUND,
            data=result,
        )
