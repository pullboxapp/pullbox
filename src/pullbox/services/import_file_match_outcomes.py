"""Outcome mutation helpers for import file matching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pullbox.core.release_parser import parse_release_title
from pullbox.models.import_job import ImportedFileStatus
from pullbox.models.issue import IssueType
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
    PROVIDER_ZERO_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_ZERO_ISSUE_PLACEHOLDER_METHOD,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries
    from pullbox.models.issue import Issue
    from pullbox.services.import_duplicates import DuplicateMergeProfile
    from pullbox.services.import_file_match_candidates import FileMatchCandidate

    LogEventFunc = Callable[..., Awaitable[None]]


class DuplicateTargetStateFunc(Protocol):
    """Return the duplicate-series target state for a matched library issue."""

    def __call__(self, issue: Issue) -> str: ...


@dataclass(frozen=True, slots=True)
class FileMatchLogEvent:
    """Structured log event emitted after mutating one imported-file match outcome."""

    name: str
    message: str
    data: dict[str, Any]
    level: str = "DEBUG"


def apply_matched_file_outcome(
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
    match_candidate: FileMatchCandidate,
    *,
    duplicate_series: bool,
    duplicate_target_state: DuplicateTargetStateFunc,
) -> FileMatchLogEvent:
    """Apply accepted file-match status/diagnostics and return its log event."""
    imp_file.matched_issue_id = match_candidate.matched_issue_id
    imp_file.matched_issue_cv_id = match_candidate.matched_issue_cv_id
    imp_file.match_confidence = match_candidate.confidence
    imp_file.match_method = match_candidate.method

    if duplicate_series and match_candidate.matched_issue is not None:
        matched_issue = match_candidate.matched_issue
        if match_candidate.has_library_file:
            imp_file.status = ImportedFileStatus.ALREADY_OWNED
            imp_file.include_in_import = False
            imp_file.diagnostics = _duplicate_file_diagnostics(
                matched_issue,
                target_state="already_owned",
            )
            return FileMatchLogEvent(
                name="import_duplicate_file_already_owned",
                message=f"Duplicate file already owned: {imp_file.file_name}",
                data={
                    "file_name": imp_file.file_name,
                    "issue_id": matched_issue.id,
                    "issue_number": matched_issue.issue_number,
                    "series": imp_series.raw_series_name,
                },
            )

        target_state = duplicate_target_state(matched_issue)
        imp_file.status = ImportedFileStatus.MATCHED
        imp_file.include_in_import = False
        imp_file.diagnostics = _duplicate_file_diagnostics(
            matched_issue,
            target_state=target_state,
        )
        return FileMatchLogEvent(
            name="import_duplicate_file_importable_match",
            message=f"Duplicate file matched to {target_state} issue: {imp_file.file_name}",
            data={
                "file_name": imp_file.file_name,
                "issue_id": matched_issue.id,
                "issue_number": matched_issue.issue_number,
                "target_state": target_state,
                "series": imp_series.raw_series_name,
            },
        )

    imp_file.status = ImportedFileStatus.MATCHED
    imp_file.include_in_import = False
    if (
        match_candidate.method
        in {
            PROVIDER_ZERO_ISSUE_PLACEHOLDER_METHOD,
            PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
        }
        and match_candidate.synthetic_issue_type is not None
    ):
        provisional = match_candidate.method == PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
        imp_file.diagnostics = {
            "kind": (
                PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND
                if provisional
                else PROVIDER_ZERO_ISSUE_PLACEHOLDER_KIND
            ),
            "target_state": (
                "provisional_issue_target" if provisional else "synthetic_issue_target"
            ),
            "target_series_cv_id": imp_series.user_selected_cv_id or imp_series.cv_id,
            "target_series_title": imp_series.cv_title or imp_series.raw_series_name,
            "target_series_issue_count": imp_series.cv_issue_count,
            "target_issue_number": match_candidate.target_issue_number,
            "target_issue_type": match_candidate.synthetic_issue_type.value,
            "target_issue_title": match_candidate.target_issue_title,
            "rejection_reason": (
                "Mylar identifies the series and local issue number; ComicVine metadata "
                "will hydrate after import."
                if provisional
                else "ComicVine has a matching one-shot/special volume but no issue rows yet."
            ),
        }
    else:
        imp_file.diagnostics = _target_issue_summary_diagnostics(
            imp_file,
            imp_series,
            match_candidate,
        )
    return FileMatchLogEvent(
        name="import_file_match_detail",
        message=f"File matched: {imp_file.file_name}",
        data={
            "file_name": imp_file.file_name,
            "parsed_issue_number": imp_file.parsed_issue_number,
            "method": match_candidate.method,
            "confidence": match_candidate.confidence,
            "series": imp_series.raw_series_name,
        },
    )


def _target_issue_summary_diagnostics(
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
    match_candidate: FileMatchCandidate,
) -> dict[str, Any]:
    """Persist the review-time issue target so Step 4 can avoid a refetch."""
    if match_candidate.matched_issue_cv_id is None:
        return {}
    issue_number = match_candidate.target_issue_number or imp_file.parsed_issue_number
    if issue_number is None:
        return {}
    issue_type = _target_issue_summary_type(
        imp_file,
        imp_series,
        match_candidate,
        issue_number=issue_number,
    )
    target_summary: dict[str, Any] = {
        "provider_id": str(match_candidate.matched_issue_cv_id),
        "issue_number": float(issue_number),
        "title": match_candidate.target_issue_title,
        "release_date": None,
        "cover_url": None,
        "issue_type": issue_type.value,
    }
    return {
        "target_issue_summary": target_summary,
    }


def _target_issue_summary_type(
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
    match_candidate: FileMatchCandidate,
    *,
    issue_number: float,
) -> IssueType:
    if match_candidate.matched_issue is not None:
        return match_candidate.matched_issue.issue_type
    if match_candidate.synthetic_issue_type is not None:
        return match_candidate.synthetic_issue_type

    source_issue_type = _source_issue_type_from_import(imp_file, imp_series)
    if source_issue_type is None or source_issue_type == IssueType.ISSUE:
        return IssueType.ISSUE

    # Numbered multi-entry graphic-novel imports behave like collected volumes
    # in the library even when the release tag says "Graphic Novel".
    if (
        source_issue_type in {IssueType.GN, IssueType.OGN}
        and int(getattr(imp_series, "file_count", 0) or 0) > 1
        and issue_number > 0
    ):
        return IssueType.VOLUME
    return source_issue_type


def _source_issue_type_from_import(
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
) -> IssueType | None:
    series_diagnostics = imp_series.diagnostics if isinstance(imp_series.diagnostics, dict) else {}
    issue_type = _coerce_issue_type(series_diagnostics.get("source_issue_type"))
    if issue_type is not None:
        return issue_type

    file_diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    issue_type = _coerce_issue_type(file_diagnostics.get("source_issue_type"))
    if issue_type is not None:
        return issue_type

    source_metadata = file_diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    issue_type = _coerce_issue_type(
        filename_parse.get("issue_type") if isinstance(filename_parse, dict) else None
    )
    if issue_type is not None:
        return issue_type

    parsed = parse_release_title(imp_file.file_name or "")
    return parsed.issue_type if parsed is not None else None


def _coerce_issue_type(raw_issue_type: object) -> IssueType | None:
    if not raw_issue_type:
        return None
    try:
        return IssueType(str(raw_issue_type))
    except ValueError:
        return None


def apply_unmatched_file_outcome(
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
    *,
    duplicate_series: bool,
    duplicate_merge_profile: DuplicateMergeProfile | None,
    metadata_conflict: dict[str, Any] | None,
) -> FileMatchLogEvent:
    """Apply unmatched file status/diagnostics and return its log event."""
    imp_file.status = ImportedFileStatus.NO_MATCH
    imp_file.include_in_import = False
    existing_diagnostics = dict(imp_file.diagnostics or {})

    if (
        duplicate_series
        and duplicate_merge_profile is not None
        and duplicate_merge_profile.single_owned_shortcut_issue is not None
    ):
        shortcut_issue = duplicate_merge_profile.single_owned_shortcut_issue
        imp_file.matched_issue_id = shortcut_issue.id
        imp_file.matched_issue_cv_id = shortcut_issue.comicvine_id
        imp_file.match_confidence = "medium"
        imp_file.match_method = "single_owned_shortcut"
        imp_file.status = ImportedFileStatus.ALREADY_OWNED
        imp_file.include_in_import = False
        imp_file.diagnostics = {
            **_duplicate_file_diagnostics(shortcut_issue, target_state="already_owned"),
            "resolution_reason": "single_owned_non_standard_ended_series",
        }
        return FileMatchLogEvent(
            name="import_duplicate_file_single_issue_already_owned",
            message=(
                "Duplicate one-shot/graphic-novel file treated as already owned: "
                f"{imp_file.file_name}"
            ),
            data={
                "file_name": imp_file.file_name,
                "issue_id": shortcut_issue.id,
                "issue_number": shortcut_issue.issue_number,
                "series": imp_series.raw_series_name,
            },
        )

    if duplicate_series:
        actionable = bool(
            duplicate_merge_profile.actionable if duplicate_merge_profile is not None else False
        )
        next_diagnostics = metadata_conflict or {
            "kind": "duplicate_series_file",
            "target_state": (
                "no_importable_targets"
                if duplicate_merge_profile is not None and not duplicate_merge_profile.actionable
                else "no_match"
            ),
            "actionable_duplicate_merge": actionable,
            "existing_issue_count": (
                duplicate_merge_profile.existing_issue_count
                if duplicate_merge_profile is not None
                else 0
            ),
            "owned_issue_count": (
                duplicate_merge_profile.owned_issue_count
                if duplicate_merge_profile is not None
                else 0
            ),
        }
        imp_file.diagnostics = {**existing_diagnostics, **next_diagnostics}
        informational_only = (
            duplicate_merge_profile is not None and not duplicate_merge_profile.actionable
        )
        return FileMatchLogEvent(
            name=(
                "import_file_metadata_conflict"
                if metadata_conflict is not None
                else (
                    "import_duplicate_file_informational_only"
                    if informational_only
                    else "import_duplicate_file_no_match"
                )
            ),
            message=(
                f"Conflicting metadata blocked auto-match: {imp_file.file_name}"
                if metadata_conflict is not None
                else (
                    f"Duplicate file is informational only: {imp_file.file_name}"
                    if informational_only
                    else f"Duplicate file needs review: {imp_file.file_name}"
                )
            ),
            data={
                "file_name": imp_file.file_name,
                "parsed_issue_number": imp_file.parsed_issue_number,
                "series": imp_series.raw_series_name,
                "diagnostics": metadata_conflict if metadata_conflict is not None else None,
            },
        )

    imp_file.diagnostics = (
        {**existing_diagnostics, **metadata_conflict}
        if metadata_conflict is not None
        else existing_diagnostics
    )
    return FileMatchLogEvent(
        name=(
            "import_file_metadata_conflict"
            if metadata_conflict is not None
            else "import_file_no_match_detail"
        ),
        message=(
            f"Conflicting metadata blocked auto-match: {imp_file.file_name}"
            if metadata_conflict is not None
            else f"No match for file: {imp_file.file_name}"
        ),
        data={
            "file_name": imp_file.file_name,
            "parsed_issue_number": imp_file.parsed_issue_number,
            "series": imp_series.raw_series_name,
            "diagnostics": metadata_conflict if metadata_conflict is not None else None,
        },
    )


async def apply_and_log_file_match_outcome(
    *,
    session: AsyncSession,
    job_id: int,
    imp_file: ImportedFile,
    imp_series: ImportedSeries,
    match_candidate: FileMatchCandidate | None,
    duplicate_series: bool,
    duplicate_target_state: DuplicateTargetStateFunc,
    duplicate_merge_profile: DuplicateMergeProfile | None,
    metadata_conflict: dict[str, Any] | None,
    log_event: LogEventFunc,
) -> FileMatchLogEvent:
    """Apply the file-match outcome and emit its structured log event."""
    if match_candidate is not None:
        file_match_event = apply_matched_file_outcome(
            imp_file,
            imp_series,
            match_candidate,
            duplicate_series=duplicate_series,
            duplicate_target_state=duplicate_target_state,
        )
    else:
        file_match_event = apply_unmatched_file_outcome(
            imp_file,
            imp_series,
            duplicate_series=duplicate_series,
            duplicate_merge_profile=duplicate_merge_profile,
            metadata_conflict=metadata_conflict,
        )
    await log_event(
        session,
        job_id,
        file_match_event.level,
        file_match_event.name,
        message=file_match_event.message,
        **file_match_event.data,
    )
    return file_match_event


def _duplicate_file_diagnostics(issue: Issue, *, target_state: str) -> dict[str, Any]:
    return {
        "kind": "duplicate_series_file",
        "target_state": target_state,
        "target_issue_id": issue.id,
        "target_issue_cv_id": issue.comicvine_id,
        "target_issue_number": issue.issue_number,
        "target_issue_title": issue.title,
    }
