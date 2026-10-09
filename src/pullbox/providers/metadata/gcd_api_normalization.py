"""Validate GCD v2 records without treating partial dates or covers as known."""

from __future__ import annotations

import html
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind, MetadataSource
from pullbox.schemas.metadata_sources import (
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
)

SOURCE = MetadataSource.GCD_API_V2


def object_row(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a GCD record")
    return cast("dict[str, object]", value)


def external_id(value: object) -> str:
    if type(value) not in {int, str}:
        raise ValueError("Expected a positive GCD identity")
    return ExternalIdentityRef(
        SOURCE.identity_namespace, MetadataEntityKind.SERIES, str(value)
    ).external_id


def _text(value: object, *, required: bool = False, limit: int = 500) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError("Expected bounded GCD text")
    return value.strip() or None


def _integer(value: object, *, year: bool = False) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= (9999 if year else 1_000_000_000):
        raise ValueError("Expected a nonnegative GCD integer")
    return value or None if year else value


def _updated(value: object) -> datetime | None:
    text = _text(value, limit=48)
    if text is None:
        return None
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        raise ValueError("Expected a timezone-aware GCD timestamp")
    return result.astimezone(UTC)


def _date(value: object) -> date | None:
    text = _text(value, limit=10)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        # GCD legitimately records unknown components, including YYYY-MM-00.
        return None


def _description(value: object) -> str | None:
    text = _text(value, limit=20000)
    return html.escape(text) if text else None


def series(value: object) -> ProviderSeriesRead:
    row = object_row(value)
    identifier = external_id(row.get("id"))
    publisher = row.get("publisher")
    return ProviderSeriesRead(
        source=SOURCE,
        identity_namespace=SOURCE.identity_namespace,
        external_id=identifier,
        title=_text(row.get("name"), required=True) or "",
        sort_title=_text(row.get("sort_name")),
        year_start=_integer(row.get("year_began"), year=True),
        year_end=_integer(row.get("year_ended"), year=True),
        publisher=_text(object_row(publisher).get("name")) if publisher is not None else None,
        issue_count=_integer(row.get("issue_count")),
        language=_text(row.get("language"), limit=20),
        description=_description(row.get("notes")),
        resource_url=f"https://www.comics.org/series/{identifier}/",
        source_updated_at=_updated(row.get("modified")),
    )


def issue(value: object) -> ProviderIssueRead:
    row = object_row(value)
    identifier = external_id(row.get("id"))
    parent = external_id(object_row(row.get("series")).get("id"))
    number = _text(row.get("number"), required=True, limit=320) or ""
    try:
        key = normalize_issue_number_text(number)
    except ValueError:
        key = None
    pages = _text(row.get("page_count"), limit=32)
    decimal_pages = Decimal(pages) if pages is not None else None
    page_count = None
    if decimal_pages is not None:
        if not decimal_pages.is_finite() or not 0 <= decimal_pages <= 1_000_000_000:
            raise ValueError("Invalid GCD page count")
        if decimal_pages == decimal_pages.to_integral_value():
            page_count = int(decimal_pages)
    return ProviderIssueRead(
        source=SOURCE,
        identity_namespace=SOURCE.identity_namespace,
        external_id=identifier,
        series_external_id=parent,
        issue_number_text=number,
        issue_number_key=key,
        title=_text(row.get("title")),
        description=_description(row.get("notes")),
        cover_date=_date(row.get("key_date")),
        store_date=_date(row.get("on_sale_date")),
        page_count=page_count,
        resource_url=f"https://www.comics.org/issue/{identifier}/",
        source_updated_at=_updated(row.get("modified")),
    )


def story_arc(value: object) -> ProviderStoryArcRead:
    row = object_row(value)
    identifier = external_id(row.get("id"))
    # The detail's primary stories exclude some reprints and are not a member catalog.
    # Only the paginated native issue endpoint establishes complete membership.
    return ProviderStoryArcRead(
        source=SOURCE,
        identity_namespace=SOURCE.identity_namespace,
        external_id=identifier,
        title=_text(row.get("name"), required=True) or "",
        description=_description(row.get("notes")),
        resource_url=f"https://www.comics.org/story_arc/{identifier}/",
        source_updated_at=_updated(row.get("modified")),
        warnings=[
            "GCD publication order is a starting point, not a curated reading order. "
            "Review it before Add.",
            "GCD story associations may include reprints. Review which issues belong in your arc.",
        ],
    )
