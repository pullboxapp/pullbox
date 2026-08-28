"""Duplicate-series and duplicate-file helper contracts for import workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
from pullbox.models.issue import Issue, IssueStatus, is_non_standard_issue_type
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType


@dataclass(slots=True)
class DuplicateMergeProfile:
    """Describe whether an existing-library duplicate still has import opportunities."""

    actionable: bool
    fully_owned: bool
    existing_issue_count: int
    owned_issue_count: int
    single_owned_shortcut_issue: Issue | None = None


_CONFIDENCE_RANKS = {"high": 3, "medium": 2, "low": 1}
_CV_ONLY_MATCH_METHODS = frozenset(
    {
        "comicinfo_cv_id",
        "folder_cv_id",
        "exact_title_year",
        "explicit_issue_series_split",
        "mylar3_cv_id",
        "user_override",
    }
)
_NON_SCANNER_SUFFIX_TOKENS = frozenset(
    {
        "annual",
        "compendium",
        "deluxe",
        "gn",
        "graphic",
        "hardcover",
        "hc",
        "novel",
        "ogn",
        "omnibus",
        "one",
        "paperback",
        "shot",
        "special",
        "tpb",
        "trade",
        "vol",
        "volume",
    }
)
_TRAILING_SCANNER_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\s*[\[(](?P<suffix>[^)\]]+)[)\]]\s*$")
_SCANNER_STYLE_SUFFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_. '-]{2,}$")


def confidence_rank(confidence: str | None) -> int:
    """Convert confidence string to sortable integer."""
    return _CONFIDENCE_RANKS.get(confidence or "", 0)


def is_duplicate_series(item: ImportedSeries | None) -> bool:
    """Return True when the imported series is an existing-library duplicate."""
    return bool(
        item is not None
        and item.status == ImportSeriesStatus.DUPLICATE
        and item.series_id is not None
    )


def duplicate_target_state(issue: Issue) -> str:
    """Describe whether a duplicate-series issue is wanted or merely missing."""
    if issue.status in {IssueStatus.WANTED, IssueStatus.DOWNLOADING}:
        return "wanted"
    return "missing"


def duplicate_merge_is_actionable(item: ImportedSeries | None) -> bool:
    """Return True when a duplicate series still has wanted/missing import targets."""
    if item is None or not is_duplicate_series(item):
        return False
    has_importable_files = bool((item.files_matched or 0) > 0 or (item.files_conflict or 0) > 0)
    if has_importable_files:
        return True
    diagnostics = dict(item.diagnostics or {})
    if "actionable_duplicate_merge" in diagnostics:
        return bool(diagnostics["actionable_duplicate_merge"])
    return False


def build_duplicate_merge_profile(
    existing_series: Series | None,
    issue_entries: list[tuple[Issue, bool]],
    *,
    incoming_file_count: int,
) -> DuplicateMergeProfile:
    """Summarize whether a duplicate series has legitimate wanted-only merge targets."""
    existing_issue_count = len(issue_entries)
    owned_issue_count = sum(1 for _issue, has_library_file in issue_entries if has_library_file)
    actionable = any(not has_library_file for _issue, has_library_file in issue_entries)
    fully_owned = (
        existing_issue_count > 0
        and owned_issue_count == existing_issue_count
        and _catalog_can_prove_full_ownership(existing_series, existing_issue_count)
    )

    single_owned_shortcut_issue: Issue | None = None
    if (
        existing_series is not None
        and fully_owned
        and existing_issue_count == 1
        and incoming_file_count >= 1
        and existing_series.status == SeriesStatus.ENDED
        and (
            existing_series.series_type != SeriesType.STANDARD
            or is_non_standard_issue_type(issue_entries[0][0].issue_type)
        )
    ):
        single_owned_shortcut_issue = issue_entries[0][0]

    return DuplicateMergeProfile(
        actionable=actionable,
        fully_owned=fully_owned,
        existing_issue_count=existing_issue_count,
        owned_issue_count=owned_issue_count,
        single_owned_shortcut_issue=single_owned_shortcut_issue,
    )


def _catalog_can_prove_full_ownership(
    existing_series: Series | None,
    existing_issue_count: int,
) -> bool:
    """Return whether local issue rows are complete enough to call a series fully owned."""
    if existing_series is None:
        return True

    expected_issue_count = int(existing_series.issue_count or 0)
    raw_state = existing_series.issue_catalog_state
    if raw_state is None:
        catalog_state = IssueCatalogState.COMPLETE
    else:
        catalog_state = (
            raw_state
            if isinstance(raw_state, IssueCatalogState)
            else IssueCatalogState(str(raw_state).lower())
        )
    if catalog_state != IssueCatalogState.COMPLETE and expected_issue_count > 0:
        return False
    return expected_issue_count <= 0 or existing_issue_count >= expected_issue_count


def logical_series_group_key(
    item: ImportedSeries,
    *,
    prefer_resolved_cv_only: bool = False,
) -> tuple[object, ...] | None:
    """Return the logical target key used to consolidate repeated review buckets."""
    if item.status == ImportSeriesStatus.DUPLICATE and item.series_id is not None:
        return ("duplicate", item.series_id)
    resolved_cv_id = item.user_selected_cv_id or item.cv_id
    if item.status == ImportSeriesStatus.MATCHED and resolved_cv_id is not None:
        if prefer_resolved_cv_only or _should_group_matched_series_by_resolved_cv(item):
            return ("matched", resolved_cv_id)
        normalized_name = NameMatcher.normalize(item.raw_series_name or "")
        if normalized_name:
            return ("matched", resolved_cv_id, normalized_name)
    return None


def _should_group_matched_series_by_resolved_cv(item: ImportedSeries) -> bool:
    """Return true when the match source is strong enough to merge by CV ID alone."""
    if item.user_selected_cv_id is not None:
        return True
    method = (item.cv_match_method or "").lower()
    if method in _CV_ONLY_MATCH_METHODS:
        return True
    return method == "fuzzy_title" and _raw_name_has_scanner_suffix(
        item.raw_series_name,
        item.cv_title,
    )


def _raw_name_has_scanner_suffix(raw_name: str | None, cv_title: str | None) -> bool:
    """Detect split buckets caused by scanner groups left in the parsed series name."""
    if not raw_name or not cv_title:
        return False
    match = _TRAILING_SCANNER_SUFFIX_RE.match(raw_name.strip())
    if match is None:
        return False

    if NameMatcher.normalize(match.group("base")) != NameMatcher.normalize(cv_title):
        return False

    suffix = match.group("suffix").strip()
    normalized_suffix = NameMatcher.normalize(suffix)
    if not normalized_suffix:
        return False
    if set(normalized_suffix.split()) & _NON_SCANNER_SUFFIX_TOKENS:
        return False
    if re.fullmatch(r"\d{4}|of\s+\d+|\d+\s+covers?", suffix, re.IGNORECASE):
        return False
    return bool(_SCANNER_STYLE_SUFFIX_RE.fullmatch(suffix))


def preferred_file_sort_key(item: ImportedFile) -> tuple[int, int, int, int]:
    """Prefer richer metadata, stronger confidence, larger files, then older IDs."""
    return (
        1 if item.has_comicinfo else 0,
        confidence_rank(item.match_confidence),
        item.file_size,
        -item.id,
    )


def duplicate_target_key(item: ImportedFile) -> tuple[str, int | float] | None:
    """Return the issue-level target identity used for duplicate-copy detection."""
    if item.matched_issue_id is not None:
        return ("issue_id", int(item.matched_issue_id))
    if item.matched_issue_cv_id is not None:
        return ("issue_cv_id", int(item.matched_issue_cv_id))
    if item.parsed_issue_number is not None:
        return ("parsed_issue", float(item.parsed_issue_number))
    return None
