"""Build provider-free folder story-arc evidence from staged import rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.story_arc_ordering import extract_story_arc_order_prefix
from pullbox.services.import_story_arc_detection import (
    FolderArcDetection,
    FolderArcFileEvidence,
    detect_folder_story_arc,
)

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile


def detect_imported_folder_story_arc(
    *,
    folder_label: str,
    files: Sequence[ImportedFile],
    confirmed_order_pattern: bool = False,
) -> FolderArcDetection:
    """Classify one complete staged folder cohort without I/O or providers."""
    evidence = tuple(_evidence_from_imported_file(item) for item in files)
    return detect_folder_story_arc(
        folder_label=folder_label,
        files=evidence,
        confirmed_order_pattern=confirmed_order_pattern,
    )


def _evidence_from_imported_file(imp_file: ImportedFile) -> FolderArcFileEvidence:
    diagnostics = _mapping(imp_file.diagnostics)
    source_metadata = _mapping(diagnostics.get("source_metadata")) or diagnostics
    archive_evidence = _mapping(source_metadata.get("archive_member_evidence")) or _mapping(
        diagnostics.get("archive_member_evidence")
    )
    comicinfo = _mapping(archive_evidence.get("comicinfo")) or _mapping(
        source_metadata.get("comicinfo")
    )

    story_arc = _optional_text(comicinfo.get("story_arc"))
    story_arc_number = _optional_text(comicinfo.get("story_arc_number"))
    story_arc_number_source = "comicinfo" if story_arc_number is not None else None
    if story_arc_number is None:
        prefix = extract_story_arc_order_prefix(str(imp_file.file_name or ""))
        if prefix is not None:
            story_arc_number = prefix.reading_order_raw
            story_arc_number_source = "filename_prefix"

    issue_number = _optional_text(comicinfo.get("number")) or _optional_text(
        imp_file.issue_number_raw
    )
    if issue_number is None and imp_file.parsed_issue_number is not None:
        issue_number = format_issue_number(imp_file.parsed_issue_number)

    return FolderArcFileEvidence(
        relative_path=str(imp_file.file_name or ""),
        series=_optional_text(comicinfo.get("series")) or _optional_text(imp_file.parsed_series),
        issue_number=issue_number,
        story_arc=story_arc,
        story_arc_number=story_arc_number,
        story_arc_number_source=story_arc_number_source,
        evidence_complete=not isinstance(diagnostics.get("safety_block"), Mapping),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
