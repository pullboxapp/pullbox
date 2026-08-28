"""Tests for import file-matching result summarization."""

from __future__ import annotations

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.services.import_duplicates import DuplicateMergeProfile
from pullbox.services.import_file_match_results import apply_file_match_series_summary


def test_file_match_summary_applies_per_series_counts() -> None:
    series = ImportedSeries(
        raw_series_name="Absolute Wonder Woman",
        status=ImportSeriesStatus.MATCHED,
    )
    files = [
        ImportedFile(status=ImportedFileStatus.MATCHED),
        ImportedFile(status=ImportedFileStatus.CONFIRMED),
        ImportedFile(status=ImportedFileStatus.ALREADY_OWNED),
        ImportedFile(status=ImportedFileStatus.DUPLICATE_FILE),
        ImportedFile(status=ImportedFileStatus.NO_MATCH),
        ImportedFile(status=ImportedFileStatus.CONFLICT),
    ]

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=0.88,
    )

    assert summary.found == 6
    assert summary.matched == 2
    assert summary.duplicate == 1
    assert summary.already_owned == 1
    assert summary.no_match == 1
    assert summary.conflict == 1
    assert summary.series_invalidated is False
    assert series.files_total == 6
    assert series.files_matched == 2
    assert series.files_already_owned == 1
    assert series.files_duplicate == 1
    assert series.files_no_match == 1
    assert series.files_conflict == 1


def test_file_match_summary_invalidates_metadata_conflicted_series() -> None:
    series = ImportedSeries(
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        status=ImportSeriesStatus.MATCHED,
        diagnostics={"previous": "kept"},
        cv_title="Absolute Wonder Woman",
        cv_year=2024,
        cv_publisher="DC Comics",
        cv_issue_count=19,
        cv_match_score=0.93,
    )
    files = [
        ImportedFile(
            file_name="Absolute Wonder Woman 019.cbz",
            status=ImportedFileStatus.NO_MATCH,
            diagnostics={
                "kind": "metadata_conflict",
                "rejection_reason": "Issue number does not match ComicInfo metadata.",
            },
        )
    ]

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=0.88,
    )

    assert summary.series_invalidated is True
    assert series.status == ImportSeriesStatus.NO_MATCH
    assert series.diagnostics["previous"] == "kept"
    assert series.diagnostics["kind"] == "series_no_match"
    assert series.diagnostics["reason"] == "file_metadata_conflict"
    assert series.diagnostics["normalized_query"] == "absolute wonder woman"
    assert series.diagnostics["threshold"] == 0.88
    assert series.diagnostics["top_candidates"] == [
        {
            "title": "Absolute Wonder Woman",
            "year": 2024,
            "publisher": "DC Comics",
            "issue_count": 19,
            "score_pct": 93,
            "rejection_reasons": ["Issue number does not match ComicInfo metadata."],
        }
    ]
    assert series.diagnostics["conflicting_files"] == [
        {
            "file_name": "Absolute Wonder Woman 019.cbz",
            "rejection_reason": "Issue number does not match ComicInfo metadata.",
        }
    ]
    assert summary.invalidation_diagnostics == series.diagnostics


def test_file_match_summary_invalidates_plain_unmatched_series() -> None:
    series = ImportedSeries(
        raw_series_name="test 2 copy",
        status=ImportSeriesStatus.MATCHED,
        cv_id=12345,
        cv_title="Test",
        cv_year=2019,
        cv_publisher="DC Comics",
        cv_issue_count=1,
        cv_url="https://comicvine.gamespot.com/test/4050-12345/",
        cv_match_score=0.735,
        cv_match_method="fuzzy_title",
    )
    files = [
        ImportedFile(
            file_name="Secret War (Panini digital).cbr",
            parsed_series="Secret War",
            status=ImportedFileStatus.NO_MATCH,
            diagnostics={},
        )
    ]

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=0.70,
    )

    assert summary.series_invalidated is True
    assert series.status == ImportSeriesStatus.NO_MATCH
    assert series.cv_id is None
    assert series.cv_title is None
    assert series.cv_year is None
    assert series.cv_publisher is None
    assert series.cv_issue_count is None
    assert series.cv_url is None
    assert series.cv_match_score is None
    assert series.cv_match_method is None
    assert series.diagnostics["kind"] == "series_no_match"
    assert series.diagnostics["reason"] == "file_no_match"
    assert series.diagnostics["top_candidates"] == [
        {
            "title": "Test",
            "year": 2019,
            "publisher": "DC Comics",
            "issue_count": 1,
            "score_pct": 74,
            "rejection_reasons": [
                "Series matched, but no files could be matched to issues in that series."
            ],
        }
    ]
    assert series.diagnostics["unmatched_files"] == [
        {
            "file_name": "Secret War (Panini digital).cbr",
            "parsed_series": "Secret War",
            "parsed_issue_number": None,
        }
    ]


def test_file_match_summary_preserves_trusted_mylar_series_with_unmatched_file() -> None:
    series = ImportedSeries(
        raw_series_name="X-Men",
        status=ImportSeriesStatus.MATCHED,
        cv_id=140553,
        cv_title="X-Men",
        cv_match_score=1.0,
        cv_match_method="mylar3_cv_id",
    )
    files = [
        ImportedFile(
            file_name="X-Men Annual 001 (2023).cbz",
            parsed_series="X-Men Annual",
            parsed_issue_number=1.0,
            comicvine_issue_id=950001,
            status=ImportedFileStatus.NO_MATCH,
        )
    ]

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=0.88,
    )

    assert summary.series_invalidated is False
    assert series.status == ImportSeriesStatus.MATCHED
    assert series.cv_id == 140553
    assert series.cv_match_method == "mylar3_cv_id"


def test_file_match_summary_preserves_trusted_folder_series_with_unmatched_file() -> None:
    series = ImportedSeries(
        raw_series_name="Batman",
        status=ImportSeriesStatus.MATCHED,
        cv_id=97508,
        cv_title="Batman",
        cv_match_score=1.0,
        cv_match_method="comicinfo_cv_id",
    )
    files = [
        ImportedFile(
            file_name="Batman Annual 001 (2016).cbz",
            parsed_series="Batman Annual",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.NO_MATCH,
            diagnostics={
                "kind": "metadata_conflict",
                "conflict_type": "trusted_source_series_id_mismatch",
                "preserve_series_match": True,
            },
        )
    ]

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=0.88,
    )

    assert summary.series_invalidated is False
    assert series.status == ImportSeriesStatus.MATCHED
    assert series.cv_id == 97508
    assert series.cv_match_method == "comicinfo_cv_id"


def test_file_match_summary_updates_duplicate_series_diagnostics_from_profile() -> None:
    series = ImportedSeries(
        raw_series_name="Absolute Batman",
        status=ImportSeriesStatus.DUPLICATE,
        diagnostics={"kind": "duplicate_series"},
    )
    files = [
        ImportedFile(status=ImportedFileStatus.MATCHED),
        ImportedFile(status=ImportedFileStatus.ALREADY_OWNED),
        ImportedFile(status=ImportedFileStatus.NO_MATCH),
    ]
    profile = DuplicateMergeProfile(
        actionable=True,
        fully_owned=False,
        existing_issue_count=12,
        owned_issue_count=4,
    )

    summary = apply_file_match_series_summary(
        series,
        files,
        duplicate_series=True,
        duplicate_merge_profile=profile,
        cv_match_threshold=0.88,
    )

    assert summary.series_invalidated is False
    assert series.diagnostics == {
        "kind": "duplicate_series",
        "actionable_duplicate_merge": True,
        "existing_issue_count": 12,
        "owned_issue_count": 4,
        "fully_owned_series": False,
        "single_owned_shortcut_applied": False,
        "has_importable_files": True,
        "importable_files": 1,
        "duplicate_files": 0,
        "already_owned_files": 1,
        "no_match_files": 1,
        "conflict_files": 0,
    }


def test_file_match_summary_duplicate_series_falls_back_to_current_counts() -> None:
    series = ImportedSeries(
        raw_series_name="Absolute Flash",
        status=ImportSeriesStatus.DUPLICATE,
        diagnostics={"existing_issue_count": 3, "owned_issue_count": 1},
    )
    files = [ImportedFile(status=ImportedFileStatus.CONFLICT)]

    apply_file_match_series_summary(
        series,
        files,
        duplicate_series=True,
        duplicate_merge_profile=None,
        cv_match_threshold=0.88,
    )

    assert series.diagnostics["actionable_duplicate_merge"] is True
    assert series.diagnostics["existing_issue_count"] == 3
    assert series.diagnostics["owned_issue_count"] == 1
    assert series.diagnostics["fully_owned_series"] is False
    assert series.diagnostics["has_importable_files"] is False
    assert series.diagnostics["conflict_files"] == 1


def test_file_match_summary_duplicate_matched_files_override_stale_non_actionable_profile() -> None:
    series = ImportedSeries(
        raw_series_name="Poison Ivy",
        status=ImportSeriesStatus.DUPLICATE,
        diagnostics={
            "kind": "duplicate_series",
            "actionable_duplicate_merge": False,
            "fully_owned_series": True,
        },
    )
    files = [ImportedFile(status=ImportedFileStatus.MATCHED)]
    profile = DuplicateMergeProfile(
        actionable=False,
        fully_owned=False,
        existing_issue_count=2,
        owned_issue_count=2,
    )

    apply_file_match_series_summary(
        series,
        files,
        duplicate_series=True,
        duplicate_merge_profile=profile,
        cv_match_threshold=0.88,
    )

    assert series.diagnostics["actionable_duplicate_merge"] is True
    assert series.diagnostics["fully_owned_series"] is False
    assert series.diagnostics["has_importable_files"] is True
    assert series.diagnostics["importable_files"] == 1
