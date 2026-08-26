"""File-matching result summarization for import workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.services.import_series_match_state import clear_auto_cv_match_fields

if TYPE_CHECKING:
    from pullbox.services.import_duplicates import DuplicateMergeProfile


_TRUSTED_SERIES_MATCH_METHODS = frozenset({"mylar3_cv_id", "comicinfo_cv_id"})


@dataclass(frozen=True, slots=True)
class FileMatchSeriesSummary:
    """Counter snapshot produced after summarizing one import-series file pass."""

    found: int
    matched: int
    duplicate: int
    already_owned: int
    no_match: int
    conflict: int
    series_invalidated: bool = False
    invalidation_diagnostics: dict[str, Any] | None = None


def apply_file_match_series_summary(
    imp_series: ImportedSeries,
    files: list[ImportedFile],
    *,
    duplicate_series: bool,
    duplicate_merge_profile: DuplicateMergeProfile | None,
    cv_match_threshold: float,
) -> FileMatchSeriesSummary:
    """Apply per-series file counters and duplicate/metadata-conflict diagnostics."""
    status_counts: dict[ImportedFileStatus, int] = {}
    for imp_file in files:
        status_counts[imp_file.status] = status_counts.get(imp_file.status, 0) + 1

    matched = status_counts.get(ImportedFileStatus.MATCHED, 0) + status_counts.get(
        ImportedFileStatus.CONFIRMED,
        0,
    )
    duplicate = status_counts.get(ImportedFileStatus.DUPLICATE_FILE, 0)
    already_owned = status_counts.get(ImportedFileStatus.ALREADY_OWNED, 0)
    no_match = status_counts.get(ImportedFileStatus.NO_MATCH, 0)
    conflict = status_counts.get(ImportedFileStatus.CONFLICT, 0)

    imp_series.files_total = len(files)
    imp_series.files_matched = matched
    imp_series.files_duplicate = duplicate
    imp_series.files_already_owned = already_owned
    imp_series.files_no_match = no_match
    imp_series.files_conflict = conflict

    metadata_conflict_files = [
        f for f in files if dict(f.diagnostics or {}).get("kind") == "metadata_conflict"
    ]
    invalidation_diagnostics: dict[str, Any] | None = None
    if (
        not duplicate_series
        and imp_series.cv_match_method not in _TRUSTED_SERIES_MATCH_METHODS
        and imp_series.status == ImportSeriesStatus.MATCHED
        and imp_series.files_total > 0
        and imp_series.files_matched == 0
        and imp_series.files_conflict == 0
        and imp_series.files_no_match == imp_series.files_total
    ):
        invalidation_diagnostics = (
            _build_metadata_conflict_series_diagnostics(
                imp_series,
                metadata_conflict_files,
                cv_match_threshold=cv_match_threshold,
            )
            if metadata_conflict_files
            else _build_unmatched_files_series_diagnostics(
                imp_series,
                files,
                cv_match_threshold=cv_match_threshold,
            )
        )
        imp_series.status = ImportSeriesStatus.NO_MATCH
        imp_series.diagnostics = invalidation_diagnostics
        preserve_series_match = any(
            bool(dict(f.diagnostics or {}).get("preserve_series_match"))
            for f in metadata_conflict_files
        )
        if not preserve_series_match:
            clear_auto_cv_match_fields(imp_series)

    if duplicate_series:
        diagnostics = dict(imp_series.diagnostics or {})
        has_importable_files = imp_series.files_matched > 0
        has_conflict_files = imp_series.files_conflict > 0
        duplicate_profile_actionable = (
            duplicate_merge_profile.actionable if duplicate_merge_profile is not None else False
        )
        actionable_duplicate_merge = bool(
            duplicate_profile_actionable or has_importable_files or has_conflict_files
        )
        diagnostics.update(
            {
                "actionable_duplicate_merge": actionable_duplicate_merge,
                "existing_issue_count": (
                    duplicate_merge_profile.existing_issue_count
                    if duplicate_merge_profile is not None
                    else diagnostics.get("existing_issue_count", 0)
                ),
                "owned_issue_count": (
                    duplicate_merge_profile.owned_issue_count
                    if duplicate_merge_profile is not None
                    else diagnostics.get("owned_issue_count", 0)
                ),
                "fully_owned_series": bool(
                    duplicate_merge_profile.fully_owned and not actionable_duplicate_merge
                    if duplicate_merge_profile is not None
                    else diagnostics.get("fully_owned_series", False)
                ),
                "single_owned_shortcut_applied": bool(
                    duplicate_merge_profile.single_owned_shortcut_issue is not None
                    if duplicate_merge_profile is not None
                    else diagnostics.get("single_owned_shortcut_applied", False)
                ),
                "has_importable_files": has_importable_files,
                "importable_files": imp_series.files_matched,
                "duplicate_files": imp_series.files_duplicate,
                "already_owned_files": imp_series.files_already_owned,
                "no_match_files": imp_series.files_no_match,
                "conflict_files": imp_series.files_conflict,
            }
        )
        imp_series.diagnostics = diagnostics

    return FileMatchSeriesSummary(
        found=len(files),
        matched=matched,
        duplicate=duplicate,
        already_owned=already_owned,
        no_match=no_match,
        conflict=conflict,
        series_invalidated=invalidation_diagnostics is not None,
        invalidation_diagnostics=invalidation_diagnostics,
    )


def _build_metadata_conflict_series_diagnostics(
    imp_series: ImportedSeries,
    metadata_conflict_files: list[ImportedFile],
    *,
    cv_match_threshold: float,
) -> dict[str, Any]:
    primary_conflict = dict(metadata_conflict_files[0].diagnostics or {})
    series_diagnostics = dict(imp_series.diagnostics or {})
    series_diagnostics.update(
        {
            "kind": "series_no_match",
            "reason": "file_metadata_conflict",
            "normalized_query": NameMatcher.normalize(imp_series.raw_series_name),
            "raw_year": imp_series.raw_year,
            "threshold": cv_match_threshold,
            "top_candidates": [
                {
                    "title": imp_series.cv_title or imp_series.raw_series_name,
                    "year": imp_series.cv_year,
                    "publisher": imp_series.cv_publisher,
                    "issue_count": imp_series.cv_issue_count,
                    "score_pct": round((imp_series.cv_match_score or 0.0) * 100),
                    "rejection_reasons": [
                        primary_conflict.get(
                            "rejection_reason",
                            "Conflicting strong metadata signals blocked auto-match.",
                        )
                    ],
                }
            ],
            "conflicting_files": [
                {
                    "file_name": f.file_name,
                    "rejection_reason": dict(f.diagnostics or {}).get("rejection_reason"),
                }
                for f in metadata_conflict_files
            ],
        }
    )
    return series_diagnostics


def _build_unmatched_files_series_diagnostics(
    imp_series: ImportedSeries,
    unmatched_files: list[ImportedFile],
    *,
    cv_match_threshold: float,
) -> dict[str, Any]:
    """Explain why a series-level match was invalidated after file matching."""
    series_diagnostics = dict(imp_series.diagnostics or {})
    series_diagnostics.update(
        {
            "kind": "series_no_match",
            "reason": "file_no_match",
            "normalized_query": NameMatcher.normalize(imp_series.raw_series_name),
            "raw_year": imp_series.raw_year,
            "threshold": cv_match_threshold,
            "top_candidates": [
                {
                    "title": imp_series.cv_title or imp_series.raw_series_name,
                    "year": imp_series.cv_year,
                    "publisher": imp_series.cv_publisher,
                    "issue_count": imp_series.cv_issue_count,
                    "score_pct": round((imp_series.cv_match_score or 0.0) * 100),
                    "rejection_reasons": [
                        "Series matched, but no files could be matched to issues in that series."
                    ],
                }
            ],
            "unmatched_files": [
                {
                    "file_name": f.file_name,
                    "parsed_series": f.parsed_series,
                    "parsed_issue_number": f.parsed_issue_number,
                }
                for f in unmatched_files
            ],
        }
    )
    return series_diagnostics
