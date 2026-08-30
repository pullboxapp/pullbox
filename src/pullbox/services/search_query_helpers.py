"""Search query construction, category filtering, and result dedupe helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.issue_title import collection_title_fragment, collection_title_subtitle
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.issue import IssueType
from pullbox.providers.base import SearchQuery

if TYPE_CHECKING:
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.search_targets import IssueSearchTarget
    from pullbox.services.search_types import IssueSearchMode

# Default Newznab/Torznab categories for comic searches.
# 7020 = EBook, 7030 = Comics. The parent 7000 is intentionally excluded.
DEFAULT_COMIC_CATEGORIES = ["7020", "7030"]

# Allowlist: standard Newznab 4-digit category IDs for Books.
_ALLOWED_CATEGORY_IDS = frozenset({"7020", "7030"})
_ALLOWED_CATEGORY_NAMES = frozenset({"book", "comic", "ebook", "e-book", "magazine"})

# Collection types where release titles typically omit issue numbers.
_COLLECTION_TYPES = frozenset(
    {
        IssueType.TPB,
        IssueType.OMNIBUS,
        IssueType.HC,
        IssueType.COMPENDIUM,
        IssueType.VOLUME,
        IssueType.GN,
        IssueType.OGN,
        IssueType.DELUXE,
        IssueType.ONE_SHOT,
    }
)

# Per-type search query keywords. Types not listed use plain series+issue query.
_TYPE_QUERY_KEYWORDS: dict[str, list[str]] = {
    "annual": ["Annual"],
    "tpb": ["TPB", "Vol"],
    "hc": ["HC", "Hardcover"],
    "omnibus": ["Omnibus"],
    "gn": ["GN", "Graphic Novel"],
    "ogn": ["OGN", "Original Graphic Novel"],
    "deluxe": ["Deluxe Edition", "Deluxe"],
    "compendium": ["Compendium"],
    "volume": ["Vol", "Volume"],
}


def _is_better_release(new: ReleaseResult, existing: ReleaseResult) -> bool:
    """Return True if *new* should replace *existing* when titles match."""
    new_seeders = (new.seeders or 0) if new.is_torrent else 0
    existing_seeders = (existing.seeders or 0) if existing.is_torrent else 0
    if new_seeders != existing_seeders:
        return new_seeders > existing_seeders
    return (new.grabs or 0) > (existing.grabs or 0)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _extract_subtitle(issue_title: str) -> str | None:
    """Compatibility helper for callers that require a multi-word subtitle."""
    cleaned = re.sub(
        r"^(?:Vol(?:ume)?\.?\s*\d+|Part\s+\d+|Book\s+\d+|#?\d+)\s*[:\-\u2013\u2014]\s*",
        "",
        issue_title.strip(),
        flags=re.IGNORECASE,
    )
    if not cleaned or cleaned == issue_title.strip():
        if ":" in issue_title:
            cleaned = issue_title.split(":", 1)[1].strip()
        elif " - " in issue_title:
            cleaned = issue_title.split(" - ", 1)[1].strip()
        else:
            return None
    if cleaned and len(cleaned.split()) >= 2:
        return cleaned
    return None


def _collection_query_strings(target: IssueSearchTarget) -> list[str]:
    """Build bounded title-first queries for a collection target."""
    queries: list[str] = []
    title_fragment = collection_title_fragment(target.issue_title)
    if title_fragment:
        _append_unique(queries, _sanitize_query(f"{target.series_title} {title_fragment}"))
    for query in _build_type_queries(
        target.series_title,
        target.issue_number,
        target.issue_type,
    ):
        _append_unique(queries, query)
    return queries


def _is_comic_category(category: str | None) -> bool:
    """Check if a result's category is compatible with comics."""
    if not category:
        return True

    parts = [p.strip() for p in category.split(",") if p.strip()]
    if not parts:
        return True

    has_standard_category = False

    for part in parts:
        if part.isdigit() and len(part) == 4:
            has_standard_category = True
            if part in _ALLOWED_CATEGORY_IDS:
                return True
            continue

        if part.isdigit() and len(part) > 4:
            continue

        lower = part.lower()
        has_standard_category = True
        if any(kw in lower for kw in _ALLOWED_CATEGORY_NAMES):
            return True

    return not has_standard_category


def _sanitize_query(query: str) -> str:
    """Remove special characters that break indexer searches."""
    cleaned = re.sub(r"[;:!?]", " ", query)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _standard_issue_query_variants(series_title: str, issue_number: float | None) -> list[str]:
    """Build issue query variants for indexers with strict issue-number token matching."""
    if issue_number is None:
        return [_sanitize_query(series_title)]
    if issue_number != int(issue_number):
        return [_sanitize_query(f"{series_title} {format_issue_number(issue_number)}")]

    issue_int = int(issue_number)
    variants = [
        str(issue_int),
        f"{issue_int:02d}",
        f"#{issue_int:02d}",
        f"{issue_int:03d}",
        f"#{issue_int:03d}",
    ]
    queries: list[str] = []
    for variant in variants:
        query = _sanitize_query(f"{series_title} {variant}")
        if query not in queries:
            queries.append(query)
    return queries


def _build_type_queries(
    series_title: str,
    issue_number: float | None,
    issue_type: IssueType,
) -> list[str]:
    """Build search query strings, appending type keywords for non-standard types."""
    keywords = _TYPE_QUERY_KEYWORDS.get(issue_type.value, [])

    def _fmt_issue(num: float) -> str:
        return format_issue_number(num)

    if not keywords:
        return _standard_issue_query_variants(series_title, issue_number)

    queries: list[str] = []
    for kw in keywords:
        query = f"{series_title} {kw}"
        if issue_number is not None:
            query += f" {_fmt_issue(issue_number)}"
        queries.append(_sanitize_query(query))
    return queries


def build_issue_queries(
    target: IssueSearchTarget,
    *,
    mode: IssueSearchMode,
    force_generic: bool = False,
) -> list[SearchQuery]:
    """Build search queries for one issue target and mode."""
    issue_type = target.issue_type
    if mode == "fast":
        if issue_type == IssueType.ISSUE and not force_generic:
            return [
                SearchQuery(
                    series_title=query_string,
                    issue_number=None,
                    year=target.series_year,
                    issue_type=issue_type.value,
                )
                for query_string in _standard_issue_query_variants(
                    target.series_title,
                    target.issue_number,
                )
            ]
        if issue_type_family(issue_type) is TypeFamily.COLLECTION and not force_generic:
            return [
                SearchQuery(
                    series_title=query_string,
                    issue_number=None,
                    year=target.search_year,
                    issue_type=issue_type.value,
                )
                for query_string in _collection_query_strings(target)
            ]
        return [
            SearchQuery(
                series_title=target.series_title,
                issue_number=target.issue_number,
                year=target.search_year,
                issue_type=issue_type.value,
            )
        ]

    if force_generic:
        query_strings = _build_type_queries(
            target.series_title,
            target.issue_number,
            IssueType.ISSUE,
        )
    else:
        query_strings = _collection_query_strings(target)
        if issue_type_family(issue_type) is not TypeFamily.COLLECTION:
            query_strings = _build_type_queries(
                target.series_title,
                target.issue_number,
                issue_type,
            )

    if not force_generic and issue_type in _COLLECTION_TYPES:
        plain = _sanitize_query(target.series_title)
        if plain not in query_strings:
            query_strings.append(plain)

    if not force_generic:
        for year in (target.search_year, target.series_year):
            if year:
                _append_unique(query_strings, _sanitize_query(f"{target.series_title} {year}"))

    if not force_generic and issue_type in _COLLECTION_TYPES and target.issue_title:
        subtitle = collection_title_subtitle(target.issue_title)
        if subtitle:
            subtitle_query = _sanitize_query(f"{target.series_title} {subtitle}")
            if subtitle_query not in query_strings:
                query_strings.append(subtitle_query)

    return [
        SearchQuery(
            series_title=query_string,
            issue_number=None,
            year=target.search_year,
            issue_type=issue_type.value,
        )
        for query_string in query_strings
    ]


def build_auto_fallback_queries(target: IssueSearchTarget) -> list[SearchQuery]:
    """Build fallback queries matching the existing manual-search behavior."""
    if target.issue_type != IssueType.ISSUE:
        fallback_issue: float | None = target.issue_number
        if target.issue_type in _COLLECTION_TYPES:
            fallback_issue = None
        generic_queries = _build_type_queries(
            target.series_title,
            fallback_issue,
            IssueType.ISSUE,
        )
        return [
            SearchQuery(
                series_title=query_string,
                issue_number=None,
                year=target.search_year,
                issue_type=target.issue_type.value,
            )
            for query_string in generic_queries
        ]

    return [
        SearchQuery(
            series_title=_sanitize_query(target.series_title),
            issue_number=None,
            year=target.search_year,
            issue_type=target.issue_type.value,
        )
    ]


def _dedupe_release_results(results: list[ReleaseResult]) -> list[ReleaseResult]:
    """Deduplicate releases by URL and normalized title, keeping the better copy."""

    deduped: list[ReleaseResult] = []
    seen_urls: set[str] = set()
    title_index: dict[str, int] = {}

    for result in results:
        if result.download_url in seen_urls:
            continue
        title_key = result.title.lower().strip()
        if title_key in title_index:
            idx = title_index[title_key]
            if _is_better_release(result, deduped[idx]):
                seen_urls.discard(deduped[idx].download_url)
                seen_urls.add(result.download_url)
                deduped[idx] = result
            continue

        title_index[title_key] = len(deduped)
        seen_urls.add(result.download_url)
        deduped.append(result)

    return deduped
