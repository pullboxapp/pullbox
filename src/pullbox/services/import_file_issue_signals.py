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


def comicinfo_issue_number_raw(imp_file: ImportedFile) -> str | float | int | None:
    """Return the issue designation cached from ComicInfo.xml during discovery."""
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    source_metadata = diagnostics.get("source_metadata")
    comicinfo = source_metadata.get("comicinfo") if isinstance(source_metadata, dict) else None
    value = comicinfo.get("number") if isinstance(comicinfo, dict) else None
    return value if isinstance(value, str | float | int) else None


def comicinfo_issue_number(imp_file: ImportedFile) -> float | None:
    """Return a numeric compatibility value from saved ComicInfo.xml evidence."""
    raw_value = comicinfo_issue_number_raw(imp_file)
    normalized = normalize_issue_number(raw_value)
    if normalized is not None:
        return normalized
    if not isinstance(raw_value, str):
        return None
    bracketed_total = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*\[\s*\d+\s*\]\s*",
        raw_value,
    )
    return normalize_issue_number(bracketed_total.group(1)) if bracketed_total else None


def candidate_issue_number(imp_file: ImportedFile) -> float | None:
    """Return the best issue-number signal available for target lookup."""
    if imp_file.parsed_issue_number is not None:
        return imp_file.parsed_issue_number
    parsed_issue_number = filename_issue_number(imp_file)
    if parsed_issue_number is not None:
        return parsed_issue_number
    volume_number = volume_issue_number(imp_file)
    if volume_number is not None:
        return volume_number
    comicinfo_number = comicinfo_issue_number(imp_file)
    if comicinfo_number is not None:
        return comicinfo_number
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    comicinfo_raw = comicinfo_issue_number_raw(imp_file)
    comicinfo_number_missing = comicinfo_raw is None or (
        isinstance(comicinfo_raw, str) and not comicinfo_raw.strip()
    )
    if (
        comicinfo_number_missing
        and diagnostics.get("source_issue_type") == "one_shot"
        and diagnostics.get("issue_count_hint") == 1
        and imp_file.matched_issue_cv_id is not None
    ):
        return 1.0
    return None


def candidate_issue_number_text(imp_file: ImportedFile) -> str | None:
    """Return a validated exact issue designation when the source preserved one."""
    issue_number = candidate_issue_number(imp_file)
    raw_value = imp_file.issue_number_raw or comicinfo_issue_number_raw(imp_file)
    if not raw_value or issue_number is None:
        return None
    try:
        normalized = normalize_issue_number_text(raw_value)
    except ValueError:
        return None
    if not issue_number_text_matches_numeric(issue_number, normalized):
        return None
    return normalized
