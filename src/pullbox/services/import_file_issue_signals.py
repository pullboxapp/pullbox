"""Issue identity helpers shared by import file matching paths."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pullbox.core.issue_numbers import (
    issue_number_text_matches_numeric,
    normalize_issue_number_text,
)
from pullbox.core.release_parser import normalize_issue_number, parse_release_title

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile


def volume_issue_number(imp_file: ImportedFile) -> float | None:
    """Return a volume-derived issue number when filename parsing found no issue."""
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    source_metadata = diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    volume = filename_parse.get("volume") if isinstance(filename_parse, dict) else None
    if not volume:
        parsed = parse_release_title(imp_file.file_name or "")
        volume = parsed.volume if parsed is not None else None
    if not volume:
        return None
    normalized = normalize_issue_number(str(volume))
    if normalized is not None:
        return normalized
    match = re.search(r"(\d+(?:\.\d+)?)", str(volume))
    if match:
        return normalize_issue_number(match.group(1))
    return None


def filename_issue_number(imp_file: ImportedFile) -> float | None:
    """Return the filename parser issue number from persisted diagnostics or filename."""
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    source_metadata = diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    issue_number = filename_parse.get("issue_number") if isinstance(filename_parse, dict) else None
    normalized = normalize_issue_number(issue_number)
    if normalized is not None:
        return normalized
    parsed = parse_release_title(imp_file.file_name or "")
    return parsed.issue_number if parsed is not None else None


def candidate_issue_number(imp_file: ImportedFile) -> float | None:
    """Return the best issue-number signal available for target lookup."""
    if imp_file.parsed_issue_number is not None:
        return imp_file.parsed_issue_number
    parsed_issue_number = filename_issue_number(imp_file)
    if parsed_issue_number is not None:
        return parsed_issue_number
    return volume_issue_number(imp_file)


def candidate_issue_number_text(imp_file: ImportedFile) -> str | None:
    """Return a validated exact issue designation when the source preserved one."""
    issue_number = candidate_issue_number(imp_file)
    if not imp_file.issue_number_raw or issue_number is None:
        return None
    try:
        normalized = normalize_issue_number_text(imp_file.issue_number_raw)
    except ValueError:
        return None
    if not issue_number_text_matches_numeric(issue_number, normalized):
        return None
    return normalized
