"""Candidate selection helpers for import file matching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import volume_subtitle_hint_from_filename
from pullbox.core.type_semantics import (
    TypeFamily,
    canonical_issue_type_for_series_type,
    issue_type_family,
    title_supports_issue_like_type,
)
from pullbox.models.issue import IssueType
from pullbox.services.import_embedded_title_match import embedded_issue_number_title_match
from pullbox.services.import_file_issue_signals import candidate_issue_number
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
    PROVIDER_ZERO_ISSUE_PLACEHOLDER_METHOD,
)

SINGLE_ISSUE_COLLECTION_VOLUME_METHOD = "single_issue_collection_volume_subtitle"
SINGLE_ISSUE_EMBEDDED_NUMBER_TITLE_METHOD = "single_issue_embedded_number_title"

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile, ImportedSeries
    from pullbox.models.issue import Issue
    from pullbox.models.series import Series
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex


@dataclass(frozen=True, slots=True)
class FileMatchCandidate:
    """The issue target selected before semantic metadata validation."""

    matched_issue_id: int | None
    matched_issue_cv_id: int | None
    target_issue_number: float | None
    has_library_file: bool
    matched_issue: Issue | None
    target_issue_title: str | None
    confidence: str
    method: str
    synthetic_issue_type: IssueType | None = None


@dataclass(frozen=True, slots=True)
class FileMatchTargetContext:
    """Series/issue target fields used by semantic matching."""

    series_title: str
    series_year: int | None
    issue_number: float | None
    issue_type: IssueType
    issue_cv_id: int | None
    issue_title: str | None


def reset_file_match_state(imp_file: ImportedFile) -> None:
    """Clear transient match artifacts before re-evaluating a pending imported file."""
    imp_file.conflict_group_id = None
    imp_file.duplicate_group_id = None
    imp_file.duplicate_of_file_id = None
    imp_file.content_hash = None
    imp_file.is_preferred = False


def _coerce_issue_type(raw_issue_type: object) -> IssueType | None:
    """Return a valid issue type for persisted scanner metadata."""
    if not raw_issue_type:
        return None
    try:
        return IssueType(str(raw_issue_type))
    except ValueError:
        return None


def _source_issue_type(imp_series: ImportedSeries) -> IssueType | None:
    """Return the imported series source issue type when it is persisted."""
    diagnostics = imp_series.diagnostics if isinstance(imp_series.diagnostics, dict) else {}
    return _coerce_issue_type(diagnostics.get("source_issue_type"))


def _file_source_issue_type(imp_file: ImportedFile) -> IssueType | None:
    """Return non-standard issue type hints from file diagnostics or filename parsing."""
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    issue_type = _coerce_issue_type(diagnostics.get("source_issue_type"))
    if issue_type is None:
        source_metadata = diagnostics.get("source_metadata")
        filename_parse = (
            source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
        )
        raw_issue_type = (
            filename_parse.get("issue_type") if isinstance(filename_parse, dict) else None
        )
        issue_type = _coerce_issue_type(raw_issue_type)
    if issue_type is None:
        parsed = parse_release_title(imp_file.file_name or "")
        if parsed is None or (parsed.issue_number is not None and parsed.volume is None):
            return None
        issue_type = parsed.issue_type
    if issue_type == IssueType.ISSUE:
        return None
    return issue_type


def _target_issue_type(
    imp_series: ImportedSeries,
    imp_file: ImportedFile,
    matched_issue: Issue | None,
    target_issue_title: str | None,
    existing_series: Series | None,
) -> IssueType:
    """Return the semantic target type for file-level validation."""
    if existing_series is not None and existing_series.series_type is not None:
        series_issue_type = canonical_issue_type_for_series_type(existing_series.series_type)
        if issue_type_family(series_issue_type) == TypeFamily.COLLECTION:
            return series_issue_type
        if issue_type_family(
            series_issue_type
        ) == TypeFamily.ISSUE_LIKE and title_supports_issue_like_type(
            series_issue_type,
            existing_series.title,
            matched_issue.title if matched_issue is not None else target_issue_title,
        ):
            return series_issue_type
    if matched_issue is not None:
        return matched_issue.issue_type
    source_issue_type = _source_issue_type(imp_series)
    if (
        source_issue_type is not None
        and issue_type_family(source_issue_type) == TypeFamily.COLLECTION
    ):
        return source_issue_type
    file_issue_type = _file_source_issue_type(imp_file)
    if file_issue_type is not None:
        if issue_type_family(file_issue_type) == TypeFamily.ISSUE_LIKE:
            if title_supports_issue_like_type(
                file_issue_type,
                existing_series.title if existing_series is not None else None,
                imp_series.cv_title,
                matched_issue.title if matched_issue is not None else target_issue_title,
            ):
                return file_issue_type
            return IssueType.ISSUE
        return file_issue_type
    return IssueType.ISSUE


def select_file_match_candidate(
    imp_file: ImportedFile,
    target_index: FileMatchTargetIndex,
    *,
    series_high_confidence: bool,
    imp_series: ImportedSeries | None = None,
) -> FileMatchCandidate | None:
    """Choose the initial issue target for a file by CV ID, then issue number."""
    if imp_file.comicvine_issue_id and imp_file.comicvine_issue_id in target_index.cv_id_map:
        matched_issue_id, matched_issue_cv_id, has_library_file, matched_issue, issue_title = (
            target_index.cv_id_map[imp_file.comicvine_issue_id]
        )
        return FileMatchCandidate(
            matched_issue_id=matched_issue_id,
            matched_issue_cv_id=matched_issue_cv_id,
            target_issue_number=matched_issue.issue_number if matched_issue is not None else None,
            has_library_file=has_library_file,
            matched_issue=matched_issue,
            target_issue_title=issue_title,
            confidence="high",
            method="comicvine_id",
        )

    if imp_file.comicvine_issue_id is not None:
        return None

    issue_number = candidate_issue_number(imp_file)
    if issue_number is not None and issue_number in target_index.number_map:
        matched_issue_id, matched_issue_cv_id, has_library_file, matched_issue, issue_title = (
            target_index.number_map[issue_number]
        )
        synthetic_issue_type = target_index.synthetic_issue_types.get(issue_number)
        return FileMatchCandidate(
            matched_issue_id=matched_issue_id,
            matched_issue_cv_id=matched_issue_cv_id,
            target_issue_number=issue_number,
            has_library_file=has_library_file,
            matched_issue=matched_issue,
            target_issue_title=issue_title,
            confidence="high" if series_high_confidence else "medium",
            method=_target_match_method(target_index, issue_number, synthetic_issue_type),
            synthetic_issue_type=synthetic_issue_type,
        )

    if _can_use_single_issue_collection_volume_subtitle_match(
        imp_file,
        target_index,
        series_high_confidence=series_high_confidence,
    ):
        target_issue_number, target_entry = next(iter(target_index.number_map.items()))
        matched_issue_id, matched_issue_cv_id, has_library_file, matched_issue, issue_title = (
            target_entry
        )
        return FileMatchCandidate(
            matched_issue_id=matched_issue_id,
            matched_issue_cv_id=matched_issue_cv_id,
            target_issue_number=target_issue_number,
            has_library_file=has_library_file,
            matched_issue=matched_issue,
            target_issue_title=issue_title,
            confidence="high",
            method=SINGLE_ISSUE_COLLECTION_VOLUME_METHOD,
        )

    if _can_use_single_issue_embedded_number_title_match(
        imp_series,
        imp_file,
        target_index,
        series_high_confidence=series_high_confidence,
    ):
        target_issue_number, target_entry = next(iter(target_index.number_map.items()))
        matched_issue_id, matched_issue_cv_id, has_library_file, matched_issue, issue_title = (
            target_entry
        )
        return FileMatchCandidate(
            matched_issue_id=matched_issue_id,
            matched_issue_cv_id=matched_issue_cv_id,
            target_issue_number=target_issue_number,
            has_library_file=has_library_file,
            matched_issue=matched_issue,
            target_issue_title=issue_title,
            confidence="high",
            method=SINGLE_ISSUE_EMBEDDED_NUMBER_TITLE_METHOD,
        )

    single_synthetic_issue = (
        len(target_index.number_map) == 1
        and next(iter(target_index.number_map)) in target_index.synthetic_issue_types
    )
    if (
        (series_high_confidence or single_synthetic_issue)
        and imp_file.parsed_issue_number is None
        and imp_file.comicvine_issue_id is None
        and target_index.existing_series is None
        and len(target_index.number_map) == 1
    ):
        target_issue_number, target_entry = next(iter(target_index.number_map.items()))
        matched_issue_id, matched_issue_cv_id, has_library_file, matched_issue, issue_title = (
            target_entry
        )
        synthetic_issue_type = target_index.synthetic_issue_types.get(target_issue_number)
        return FileMatchCandidate(
            matched_issue_id=matched_issue_id,
            matched_issue_cv_id=matched_issue_cv_id,
            target_issue_number=target_issue_number,
            has_library_file=has_library_file,
            matched_issue=matched_issue,
            target_issue_title=issue_title,
            confidence="medium",
            method=_target_match_method(
                target_index,
                target_issue_number,
                synthetic_issue_type,
                fallback="single_issue_series",
            ),
            synthetic_issue_type=synthetic_issue_type,
        )

    return None


def _target_match_method(
    target_index: FileMatchTargetIndex,
    issue_number: float,
    synthetic_issue_type: IssueType | None,
    *,
    fallback: str = "issue_number",
) -> str:
    if issue_number in target_index.provisional_issue_numbers:
        return PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
    if synthetic_issue_type is not None:
        return PROVIDER_ZERO_ISSUE_PLACEHOLDER_METHOD
    return fallback


def _can_use_single_issue_collection_volume_subtitle_match(
    imp_file: ImportedFile,
    target_index: FileMatchTargetIndex,
    *,
    series_high_confidence: bool,
) -> bool:
    """Allow collection volume ordinals to match a split one-issue subtitle volume."""
    if not series_high_confidence:
        return False
    if imp_file.comicvine_issue_id is not None:
        return False
    if target_index.existing_series is not None:
        return False
    if len(target_index.number_map) != 1:
        return False
    target_issue_number = next(iter(target_index.number_map))
    if target_issue_number != 1.0:
        return False
    if target_issue_number in target_index.synthetic_issue_types:
        return False
    if volume_subtitle_hint_from_filename(imp_file.file_name or "") is None:
        return False
    file_issue_type = _file_source_issue_type(imp_file)
    return (
        file_issue_type is not None and issue_type_family(file_issue_type) == TypeFamily.COLLECTION
    )


def _can_use_single_issue_embedded_number_title_match(
    imp_series: ImportedSeries | None,
    imp_file: ImportedFile,
    target_index: FileMatchTargetIndex,
    *,
    series_high_confidence: bool,
) -> bool:
    """Allow issue ordinals embedded in a one-issue provider title to target issue #1."""
    if imp_series is None or not series_high_confidence:
        return False
    if imp_file.comicvine_issue_id is not None:
        return False
    if target_index.existing_series is not None:
        return False
    if len(target_index.number_map) != 1:
        return False
    target_issue_number = next(iter(target_index.number_map))
    if target_issue_number != 1.0:
        return False
    if target_issue_number in target_index.synthetic_issue_types:
        return False
    return embedded_issue_number_title_match(
        source_series_name=imp_file.parsed_series,
        original_title=imp_file.file_name,
        target_series_title=imp_series.cv_title or imp_series.raw_series_name,
        issue_number=candidate_issue_number(imp_file),
    )


def build_file_match_target_context(
    imp_series: ImportedSeries,
    imp_file: ImportedFile,
    candidate: FileMatchCandidate,
    *,
    existing_series: Series | None,
) -> FileMatchTargetContext:
    """Build the semantic target context for a selected file-match candidate."""
    matched_issue = candidate.matched_issue
    target_issue_type = candidate.synthetic_issue_type or _target_issue_type(
        imp_series,
        imp_file,
        matched_issue,
        candidate.target_issue_title,
        existing_series,
    )
    if existing_series is not None:
        series_title = existing_series.title
        series_year = existing_series.year_start
    else:
        series_title = imp_series.cv_title or imp_series.raw_series_name
        series_year = imp_series.cv_year or imp_series.raw_year
        if (
            matched_issue is None
            and issue_type_family(target_issue_type) == TypeFamily.ISSUE_LIKE
            and imp_series.raw_series_name
        ):
            # Annual/one-shot provider summaries often use fuller ComicVine titles than
            # the scanned release bucket. Preserve the scanned base title for semantic
            # validation once the series-level matcher has already selected the target.
            series_title = imp_series.raw_series_name
    return FileMatchTargetContext(
        series_title=series_title,
        series_year=series_year,
        issue_number=(
            matched_issue.issue_number
            if matched_issue is not None
            else candidate.target_issue_number or imp_file.parsed_issue_number
        ),
        issue_type=target_issue_type,
        issue_cv_id=candidate.matched_issue_cv_id,
        issue_title=matched_issue.title if matched_issue else candidate.target_issue_title,
    )
