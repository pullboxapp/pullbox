"""Tests for import duplicate helper contracts."""

from __future__ import annotations

from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType
from pullbox.services.import_duplicates import (
    build_duplicate_merge_profile,
    duplicate_merge_is_actionable,
    duplicate_target_key,
    duplicate_target_state,
    is_duplicate_series,
    logical_series_group_key,
    preferred_file_sort_key,
)
from pullbox.services.import_service import ImportService


def test_is_duplicate_series_requires_duplicate_status_and_series_id() -> None:
    assert is_duplicate_series(None) is False
    assert is_duplicate_series(ImportedSeries(status=ImportSeriesStatus.DUPLICATE)) is False
    assert (
        is_duplicate_series(ImportedSeries(status=ImportSeriesStatus.DUPLICATE, series_id=123))
        is True
    )
    assert (
        is_duplicate_series(ImportedSeries(status=ImportSeriesStatus.MATCHED, series_id=123))
        is False
    )


def test_duplicate_merge_is_actionable_keeps_matched_files_importable() -> None:
    series = ImportedSeries(
        status=ImportSeriesStatus.DUPLICATE,
        series_id=123,
        files_matched=2,
        diagnostics={"actionable_duplicate_merge": False},
    )

    assert duplicate_merge_is_actionable(series) is True
    assert (
        duplicate_merge_is_actionable(
            ImportedSeries(
                status=ImportSeriesStatus.DUPLICATE,
                series_id=123,
                files_matched=0,
                files_conflict=0,
                diagnostics={"actionable_duplicate_merge": False},
            )
        )
        is False
    )
    assert duplicate_merge_is_actionable(
        ImportedSeries(status=ImportSeriesStatus.DUPLICATE, series_id=123, files_conflict=1)
    )


def test_duplicate_target_state_distinguishes_wanted_from_missing() -> None:
    assert duplicate_target_state(Issue(status=IssueStatus.WANTED)) == "wanted"
    assert duplicate_target_state(Issue(status=IssueStatus.DOWNLOADING)) == "wanted"
    assert duplicate_target_state(Issue(status=IssueStatus.OWNED)) == "missing"


def test_build_duplicate_merge_profile_marks_single_owned_shortcut() -> None:
    issue = Issue(issue_type=IssueType.ISSUE)
    profile = build_duplicate_merge_profile(
        Series(status=SeriesStatus.ENDED, series_type=SeriesType.TPB),
        [(issue, True)],
        incoming_file_count=1,
    )

    assert profile.actionable is False
    assert profile.fully_owned is True
    assert profile.existing_issue_count == 1
    assert profile.owned_issue_count == 1
    assert profile.single_owned_shortcut_issue is issue


def test_build_duplicate_merge_profile_keeps_open_targets_actionable() -> None:
    owned_issue = Issue(issue_type=IssueType.ISSUE)
    missing_issue = Issue(issue_type=IssueType.ISSUE)

    profile = build_duplicate_merge_profile(
        Series(status=SeriesStatus.CONTINUING, series_type=SeriesType.STANDARD),
        [(owned_issue, True), (missing_issue, False)],
        incoming_file_count=1,
    )

    assert profile.actionable is True
    assert profile.fully_owned is False
    assert profile.existing_issue_count == 2
    assert profile.owned_issue_count == 1
    assert profile.single_owned_shortcut_issue is None


def test_build_duplicate_merge_profile_does_not_mark_partial_catalog_fully_owned() -> None:
    owned_issue = Issue(issue_type=IssueType.ISSUE)
    series = Series(
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        issue_count=45,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )

    profile = build_duplicate_merge_profile(
        series,
        [(owned_issue, True)],
        incoming_file_count=1,
    )

    assert profile.actionable is False
    assert profile.fully_owned is False
    assert profile.existing_issue_count == 1
    assert profile.owned_issue_count == 1
    assert profile.single_owned_shortcut_issue is None


def test_logical_series_group_key_preserves_duplicate_and_matched_identity() -> None:
    assert logical_series_group_key(
        ImportedSeries(status=ImportSeriesStatus.DUPLICATE, series_id=321)
    ) == ("duplicate", 321)
    assert logical_series_group_key(
        ImportedSeries(
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Absolute Wonder Woman",
            cv_id=111,
        )
    ) == ("matched", 111, "absolute wonder woman")
    assert logical_series_group_key(
        ImportedSeries(
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Absolute Wonder Woman",
            cv_id=111,
        ),
        prefer_resolved_cv_only=True,
    ) == ("matched", 111)


def test_file_sort_and_duplicate_target_keys_prefer_existing_contracts() -> None:
    preferred = ImportedFile(
        id=1,
        has_comicinfo=True,
        match_confidence="high",
        file_size=4096,
        matched_issue_id=10,
    )
    lower_confidence = ImportedFile(
        id=2,
        has_comicinfo=True,
        match_confidence="medium",
        file_size=8192,
        matched_issue_cv_id=20,
    )
    parsed_only = ImportedFile(
        id=3,
        has_comicinfo=False,
        match_confidence="low",
        file_size=1024,
        parsed_issue_number=4.0,
    )

    assert sorted(
        [parsed_only, lower_confidence, preferred],
        key=preferred_file_sort_key,
        reverse=True,
    ) == [preferred, lower_confidence, parsed_only]
    assert duplicate_target_key(preferred) == ("issue_id", 10)
    assert duplicate_target_key(lower_confidence) == ("issue_cv_id", 20)
    assert duplicate_target_key(parsed_only) == ("parsed_issue", 4.0)


def test_file_sort_prefers_exact_mylar_cross_folder_canonical() -> None:
    canonical = ImportedFile(
        id=20,
        has_comicinfo=True,
        match_confidence="medium",
        file_size=1024,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "role": "canonical",
            }
        },
    )
    ordinary = ImportedFile(
        id=10,
        has_comicinfo=True,
        match_confidence="high",
        file_size=8192,
    )

    assert max([ordinary, canonical], key=preferred_file_sort_key) is canonical


def test_import_service_duplicate_shims_remain_available() -> None:
    item = ImportedSeries(status=ImportSeriesStatus.DUPLICATE, series_id=123)

    assert ImportService._is_duplicate_series(item) is True
    assert ImportService._logical_series_group_key(item) == ("duplicate", 123)
