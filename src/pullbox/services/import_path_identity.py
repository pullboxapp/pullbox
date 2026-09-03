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
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata

if TYPE_CHECKING:
    from pathlib import Path


def same_trusted_issue(recorded: SourceMetadata, actual: SourceMetadata) -> bool:
    """Require independent exact IDs; folder names and issue numbers are not proof."""
    if (
        not recorded.comicvine_issue_id
        or recorded.comicvine_issue_id != actual.comicvine_issue_id
        or recorded.signals.get("comicvine_issue_id") != MetadataSignal.MYLAR3
        or actual.signals.get("comicvine_issue_id") != MetadataSignal.COMICINFO
        or recorded.diagnostics.get("identity_conflicts")
        or actual.diagnostics.get("identity_conflicts")
        or recorded.issue_type != actual.issue_type
    ):
        return False
    if (
        recorded.comicvine_series_id is not None
        and actual.comicvine_series_id is not None
        and recorded.comicvine_series_id != actual.comicvine_series_id
    ):
        return False
    if (
        not recorded.series_name
        or not actual.series_name
        or NameMatcher.normalize(recorded.series_name) != NameMatcher.normalize(actual.series_name)
        or recorded.issue_number != actual.issue_number
    ):
        return False
    recorded_issue = recorded.diagnostics.get("mylar3_issue")
    date = recorded_issue.get("release_date") if isinstance(recorded_issue, dict) else None
    return not (
        isinstance(date, str)
        and date[:4].isdigit()
        and actual.year is not None
        and int(date[:4]) != actual.year
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


def reconciliation_evidence(recorded: str, actual: str, issue_id: int) -> dict[str, Any]:
    return {
        "recorded_path": recorded,
        "actual_path": actual,
        "comicvine_issue_id": issue_id,
        "method": "verified_same_folder_issue_identity",
    }
