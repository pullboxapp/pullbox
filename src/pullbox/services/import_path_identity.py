"""Shared evidence and filesystem boundaries for stale source reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.filesystem_policy import is_invalid_path_text, resolve_preview_source
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from pathlib import Path


def _same_issue_type(recorded: SourceMetadata, actual: SourceMetadata) -> bool:
    if recorded.issue_type == actual.issue_type:
        return True
    # Mylar's ordinary issue table also holds numbered collected volumes.
    parsed = SourceMetadataExtractor().from_release_title(recorded.original_title)
    return bool(
        recorded.issue_type == IssueType.ISSUE
        and actual.issue_type == IssueType.VOLUME
        and parsed.issue_type == IssueType.VOLUME
        and parsed.volume is not None
        and parsed.issue_number == recorded.issue_number
    )


def _publication_year(metadata: SourceMetadata) -> int | None:
    comicinfo = metadata.diagnostics.get("comicinfo")
    year = comicinfo.get("year") if isinstance(comicinfo, dict) else None
    if isinstance(year, int) and not isinstance(year, bool) and year > 0:
        return year
    parsed = parse_release_title(metadata.original_title)
    return parsed.year if parsed is not None else None


def same_trusted_issue(recorded: SourceMetadata, actual: SourceMetadata) -> bool:
    """Require independent exact IDs; folder names and issue numbers are not proof."""
    if (
        not recorded.comicvine_issue_id
        or recorded.comicvine_issue_id != actual.comicvine_issue_id
        or recorded.signals.get("comicvine_issue_id") != MetadataSignal.MYLAR3
        or actual.signals.get("comicvine_issue_id") != MetadataSignal.COMICINFO
        or recorded.diagnostics.get("identity_conflicts")
        or actual.diagnostics.get("identity_conflicts")
        or actual.diagnostics.get("comicinfo_issue_number_ignored")
        or not _same_issue_type(recorded, actual)
    ):
        return False
    if (
        recorded.comicvine_series_id is not None
        and actual.comicvine_series_id is not None
        and recorded.comicvine_series_id != actual.comicvine_series_id
    ):
        return False
    exact_series_identity = (
        recorded.comicvine_series_id is not None
        and actual.comicvine_series_id is not None
        and recorded.comicvine_series_id == actual.comicvine_series_id
    )
    series_names_match = bool(
        recorded.series_name
        and actual.series_name
        and NameMatcher.normalize(recorded.series_name) == NameMatcher.normalize(actual.series_name)
    )
    if (not exact_series_identity and not series_names_match) or (
        recorded.issue_number != actual.issue_number
    ):
        return False
    recorded_issue = recorded.diagnostics.get("mylar3_issue")
    date = recorded_issue.get("release_date") if isinstance(recorded_issue, dict) else None
    # SourceMetadata.year may be ComicInfo.Volume (the series start year).
    publication_year = _publication_year(actual)
    return not (
        isinstance(date, str)
        and date[:4].isdigit()
        and publication_year is not None
        and int(date[:4]) != publication_year
    )


def unchanged_same_folder_pair(recorded: Path, actual: Path, signature: dict[str, Any]) -> bool:
    """A missing reference must really be absent, never unreadable or a dangling link."""
    if recorded.parent != actual.parent:
        return False
    if any(
        not path.is_absolute() or ".." in path.parts or is_invalid_path_text(str(path))
        for path in (recorded, actual)
    ):
        return False
    try:
        recorded.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    try:
        if actual.is_symlink():
            return False
        resolve_preview_source(actual)
        validate_file_identity_signature(signature, build_file_identity_signature(actual))
        return True
    except (OSError, RuntimeError, ValueError, ConfigurationError):
        return False


def reconciliation_evidence(
    recorded: str,
    actual: str,
    issue_id: int,
    *,
    recorded_series_name: str | None = None,
    actual_series_name: str | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "recorded_path": recorded,
        "actual_path": actual,
        "comicvine_issue_id": issue_id,
        "method": "verified_same_folder_issue_identity",
    }
    if (
        recorded_series_name
        and actual_series_name
        and NameMatcher.normalize(recorded_series_name) != NameMatcher.normalize(actual_series_name)
    ):
        evidence["series_name_alias"] = {
            "recorded": recorded_series_name,
            "actual": actual_series_name,
            "accepted_by": "exact_comicvine_series_and_issue_identity",
        }
    return evidence
