"""Tests for cached, provider-free folder story-arc evidence."""

from __future__ import annotations

from pullbox.models.import_job import ImportedFile
from pullbox.services.import_folder_story_arc_evidence import (
    detect_imported_folder_story_arc,
)
from pullbox.services.import_story_arc_detection import FolderArcClassification


def _file(
    name: str,
    series: str,
    *,
    arc: str | None = None,
    order: str | None = None,
    safety_blocked: bool = False,
) -> ImportedFile:
    comicinfo: dict[str, object] = {"series": series, "number": "1"}
    if arc is not None:
        comicinfo["story_arc"] = arc
    if order is not None:
        comicinfo["story_arc_number"] = order
    diagnostics: dict[str, object] = {
        "source_metadata": {
            "archive_member_evidence": {
                "member_index_scanned": True,
                "comicinfo": comicinfo,
            }
        }
    }
    if safety_blocked:
        diagnostics["safety_block"] = {"code": "archive_inspection_failed"}
    return ImportedFile(
        file_name=name,
        file_path=f"/private/source/{name}",
        parsed_series=series,
        parsed_issue_number=1.0,
        issue_number_raw="1",
        diagnostics=diagnostics,
    )


def test_cached_comicinfo_arc_evidence_classifies_split_series_without_io() -> None:
    result = detect_imported_folder_story_arc(
        folder_label="Court of Owls",
        files=(
            _file("Batman 001.cbz", "Batman", arc="Court of Owls", order="1"),
            _file("Nightwing 001.cbz", "Nightwing", arc="Court of Owls", order="2"),
        ),
    )

    assert result.classification == FolderArcClassification.STORY_ARC
    assert result.proposed_name == "Court of Owls"
    assert result.provider_calls_required is False


def test_filename_prefixes_are_weak_review_evidence_only() -> None:
    result = detect_imported_folder_story_arc(
        folder_label="Axis",
        files=(
            _file("001 - Avengers 001.cbz", "Avengers"),
            _file("002-X-Men 001.cbz", "X-Men"),
        ),
    )

    assert result.classification == FolderArcClassification.NEEDS_REVIEW
    assert result.reason == "ordered_mixed_folder_requires_confirmation"
    assert result.ordered_file_count == 2


def test_safety_blocked_member_never_silently_confirms_arc() -> None:
    result = detect_imported_folder_story_arc(
        folder_label="Court of Owls",
        files=(
            _file("001 - Batman.cbz", "Batman", arc="Court of Owls", order="1"),
            _file("002 - Nightwing.cbz", "Nightwing", safety_blocked=True),
        ),
    )

    assert result.classification == FolderArcClassification.NEEDS_REVIEW
    assert result.reason == "incomplete_arc_evidence"
