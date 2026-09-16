"""File conflict detection helpers for import review."""

from __future__ import annotations

import re
from typing import Any

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.services.import_duplicates import confidence_rank

_RELEASE_SITE_SUFFIX = re.compile(
    r"\s+(?:www\.)?[\w-]+\.(?:com|org|net|info)(?=\.(?:cbz|cbr|cb7|pdf|epub)$)",
    re.IGNORECASE,
)


def review_filename_identity(
    file_name: str, parsed_series: str | None, parsed_year: int | None
) -> tuple[str, int | None]:
    """Use dated filename evidence, not inherited Mylar series-start metadata.

    Undated filenames can contain copy counters or issue ordinals that the release
    parser folds into the title. Retain their saved identity rather than guessing.
    """
    parsed = parse_release_title(_RELEASE_SITE_SUFFIX.sub("", file_name or ""))
    if parsed is None or parsed.year is None:
        return NameMatcher.normalize(parsed_series or ""), parsed_year
    return (
        NameMatcher.normalize(parsed.series_name or parsed_series or ""),
        parsed.year if parsed.year is not None else parsed_year,
    )


def classify_conflict_group(files: list[ImportedFile]) -> str:
    """Describe whether competing copies agree about their comic identity."""
    hashes = {file.content_hash for file in files}
    if len(files) > 1 and len(hashes) == 1 and None not in hashes and "" not in hashes:
        return "identical_copy"
    identities = [
        review_filename_identity(file.file_name, file.parsed_series, file.parsed_year)
        for file in files
    ]
    names = {title for title, _ in identities if title}
    saved_names = {
        NameMatcher.normalize(file.parsed_series) for file in files if file.parsed_series
    }
    if len(names) > 1 or len(saved_names) > 1:
        return "series_mismatch"
    years = {year for _, year in identities if year}
    if len(years) > 1:
        return "year_disagreement"
    return "duplicate_copy"


def preferred_conflict_reasons(
    preferred: ImportedFile,
    others: list[ImportedFile],
) -> list[str]:
    """Explain why the preferred file won the conflict tiebreaker."""
    reasons: list[str] = []
    if preferred.has_comicinfo and any(not other.has_comicinfo for other in others):
        reasons.append("ComicInfo metadata present")
    if others:
        max_other_confidence = max(confidence_rank(other.match_confidence) for other in others)
        if confidence_rank(preferred.match_confidence) > max_other_confidence:
            reasons.append("Higher match confidence")
        max_other_size = max(other.file_size for other in others)
        if preferred.file_size > max_other_size:
            reasons.append("Largest file size")
    if not reasons:
        reasons.append("Won the metadata tiebreaker")
    return reasons


def rejected_conflict_reasons(
    candidate: ImportedFile,
    preferred: ImportedFile,
) -> list[str]:
    """Explain why a conflicting file was not selected."""
    reasons: list[str] = []
    if preferred.has_comicinfo and not candidate.has_comicinfo:
        reasons.append("No ComicInfo metadata")
    if confidence_rank(candidate.match_confidence) < confidence_rank(preferred.match_confidence):
        reasons.append("Lower match confidence than the preferred file")
    if candidate.file_size < preferred.file_size:
        reasons.append("Smaller file than the preferred file")
    if not reasons:
        reasons.append("Lost the metadata tiebreaker")
    return reasons


def issue_identity_key(imp_file: ImportedFile) -> int | float | None:
    """Return the best available issue identity for conflict grouping."""
    key: int | float | None = imp_file.matched_issue_id
    if key is None and imp_file.matched_issue_cv_id is not None:
        key = float(-imp_file.matched_issue_cv_id)
    if key is None and imp_file.parsed_issue_number is not None:
        key = -imp_file.parsed_issue_number - 0.001
    return key


def _mark_conflict_group(
    group: list[ImportedFile],
    *,
    group_counter: int,
    scope: str,
) -> dict[str, Any]:
    def sort_key(imp_file: ImportedFile) -> tuple[int, int, int]:
        return (
            1 if imp_file.has_comicinfo else 0,
            confidence_rank(imp_file.match_confidence),
            imp_file.file_size,
        )

    group.sort(key=sort_key, reverse=True)
    preferred = group[0]
    conflict_class = classify_conflict_group(group)
    suggest_keeper = conflict_class in {"duplicate_copy", "identical_copy"}
    preferred_reasons = preferred_conflict_reasons(preferred, group[1:]) if suggest_keeper else []

    for idx, imp_file in enumerate(group):
        imp_file.status = ImportedFileStatus.CONFLICT
        imp_file.conflict_group_id = group_counter
        imp_file.is_preferred = idx == 0 and suggest_keeper
        imp_file.include_in_import = False
        imp_file.diagnostics = {
            "kind": "file_conflict",
            "conflict_class": conflict_class,
            "scope": scope,
            "conflict_group_id": group_counter,
            "group_size": len(group),
            "preferred_file_id": preferred.id if suggest_keeper else None,
            "preferred_file_name": preferred.file_name if suggest_keeper else None,
            "preferred_reasons": preferred_reasons,
            "selection_basis": {
                "has_comicinfo": imp_file.has_comicinfo,
                "match_confidence": imp_file.match_confidence,
                "file_size": imp_file.file_size,
            },
            "why_not_selected": (
                []
                if idx == 0 or not suggest_keeper
                else rejected_conflict_reasons(imp_file, preferred)
            ),
            "previous_diagnostics": dict(imp_file.diagnostics or {}),
        }

    return {
        "kind": "file_conflict",
        "conflict_class": conflict_class,
        "scope": scope,
        "conflict_group_id": group_counter,
        "group_size": len(group),
        "preferred_file_id": preferred.id if suggest_keeper else None,
        "preferred_file_name": preferred.file_name if suggest_keeper else None,
        "preferred_reasons": preferred_reasons,
        "files": [
            {
                "file_id": imp_file.id,
                "file_name": imp_file.file_name,
                "is_preferred": imp_file.is_preferred,
                "why_not_selected": (imp_file.diagnostics or {}).get(
                    "why_not_selected",
                    [],
                ),
            }
            for imp_file in group
        ],
    }


def detect_conflicts(
    files: list[ImportedFile],
    group_counter: int,
    *,
    scope: str = "series_row",
) -> tuple[int, int, list[dict[str, Any]]]:
    """Group matched files by issue, mark conflicts, apply tiebreakers."""
    by_issue: dict[int | float, list[ImportedFile]] = {}
    for imp_file in files:
        if imp_file.status != ImportedFileStatus.MATCHED:
            continue
        key = issue_identity_key(imp_file)
        if key is not None:
            by_issue.setdefault(key, []).append(imp_file)

    conflict_count = 0
    group_details: list[dict[str, Any]] = []
    for _issue_id, group in by_issue.items():
        if len(group) < 2:
            continue

        group_counter += 1
        group_details.append(_mark_conflict_group(group, group_counter=group_counter, scope=scope))
        conflict_count += len(group)

    return conflict_count, group_counter, group_details


def detect_cross_series_conflicts(
    files: list[ImportedFile],
    group_counter: int,
    *,
    target_series_key_by_file_id: dict[int, tuple[str, int]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Detect issue conflicts spanning multiple import-series rows."""
    by_target_issue: dict[tuple[tuple[str, int], int | float], list[ImportedFile]] = {}
    for imp_file in files:
        if imp_file.status != ImportedFileStatus.MATCHED or imp_file.id is None:
            continue
        target_series_key = target_series_key_by_file_id.get(imp_file.id)
        issue_key = issue_identity_key(imp_file)
        if target_series_key is None or issue_key is None:
            continue
        by_target_issue.setdefault((target_series_key, issue_key), []).append(imp_file)

    conflict_count = 0
    group_details: list[dict[str, Any]] = []
    for (_target_series_key, _issue_key), group in by_target_issue.items():
        if len(group) < 2 or len({imp_file.import_series_id for imp_file in group}) < 2:
            continue
        group_counter += 1
        group_details.append(
            _mark_conflict_group(group, group_counter=group_counter, scope="cross_series")
        )
        conflict_count += len(group)

    return conflict_count, group_counter, group_details
