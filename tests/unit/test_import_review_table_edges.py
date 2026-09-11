"""Boundary coverage for the Step 3 import review table helpers."""

from __future__ import annotations

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.ui.import_review_tables import (
    _build_import_review_file_detail_row,
    _comicvine_issue_url,
    _format_import_review_issue_number,
    _get_import_review_file_order_by,
    _get_import_review_series_order_by,
    _import_review_duplicate_reason_label,
    _import_review_file_group_key,
    _import_review_matched_issue_label,
    _load_import_review_file_detail_groups,
    _load_import_review_matched_file_targets,
    _normalize_import_review_file_sort,
    _normalize_import_review_series_sort,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "confidence"),
        ("", "confidence"),
        ("bogus", "confidence"),
        ("-year", "-year"),
        ("status", "status"),
    ],
)
def test_normalize_import_review_series_sort(value: str | None, expected: str) -> None:
    assert _normalize_import_review_series_sort(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "series"),
        ("", "series"),
        ("bogus", "series"),
        ("-matched_issue", "-matched_issue"),
        ("confidence", "confidence"),
    ],
)
def test_normalize_import_review_file_sort(value: str | None, expected: str) -> None:
    assert _normalize_import_review_file_sort(value) == expected


@pytest.mark.parametrize(
    "sort",
    [
        "found_series",
        "year",
        "files",
        "cv_match",
        "cv_year",
        "cv_id",
        "confidence",
        "status",
        "-status",
        "invalid",
    ],
)
def test_series_ordering_builds_stable_sql(sort: str) -> None:
    clauses = _get_import_review_series_order_by(sort)

    assert clauses
    assert "import_series.id DESC" in str(clauses[-1])


@pytest.mark.parametrize(
    "sort",
    [
        "file_name",
        "series",
        "parsed_issue",
        "matched_issue",
        "confidence",
        "status",
        "-status",
        "invalid",
    ],
)
def test_file_ordering_builds_stable_sql(sort: str) -> None:
    clauses = _get_import_review_file_order_by(sort)

    assert clauses
    assert "import_files.id DESC" in str(clauses[-1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (1, "1"),
        (1.5, "1.5"),
        ("2", "2"),
        ("Annual", "Annual"),
        ("  ", None),
        ({"number": 3}, "{'number': 3}"),
    ],
)
def test_format_import_review_issue_number(value: object, expected: str | None) -> None:
    assert _format_import_review_issue_number(value) == expected


def test_comicvine_issue_url_uses_issue_identifier() -> None:
    assert _comicvine_issue_url(42).endswith("/4000-42/")


def test_matched_issue_label_handles_title_number_and_missing_targets() -> None:
    imp_file = ImportedFile(
        import_job_id=1,
        import_series_id=1,
        file_path="/source/Issue.cbz",
        file_name="Issue.cbz",
        file_format="cbz",
        matched_issue_id=7,
    )
    titled = Issue(series_id=1, issue_number=4, title="The Test")
    numbered = Issue(series_id=1, issue_number=5)
    title_only = Issue(series_id=1, issue_number=6, title="Finale")
    title_only.__dict__["issue_number"] = True

    assert _import_review_matched_issue_label(imp_file, {7: titled}) == "#4 The Test"
    assert _import_review_matched_issue_label(imp_file, {7: numbered}) == "#5"
    assert _import_review_matched_issue_label(imp_file, {7: title_only}) == "Finale"
    assert _import_review_matched_issue_label(imp_file, {}) is None
    imp_file.matched_issue_id = None
    assert _import_review_matched_issue_label(imp_file, {7: title_only}) is None


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        ("exact_duplicate", "another incoming file"),
        ("hash_confirmed_duplicate", "confirmed by hash"),
        ("already_owned_duplicate", "owned issue"),
        ("informational_duplicate", "non-importable"),
        ("unknown", "another incoming file"),
        (None, "another incoming file"),
    ],
)
def test_duplicate_reason_labels(reason: str | None, fragment: str) -> None:
    assert fragment in _import_review_duplicate_reason_label(reason)


@pytest.mark.parametrize(
    ("file_status", "series_status", "expected"),
    [
        (ImportedFileStatus.MATCHED, ImportSeriesStatus.MATCHED, "matched"),
        (ImportedFileStatus.CONFIRMED, ImportSeriesStatus.MATCHED, "matched"),
        (ImportedFileStatus.CONFLICT, ImportSeriesStatus.MATCHED, "conflict"),
        (ImportedFileStatus.ALREADY_OWNED, ImportSeriesStatus.MATCHED, "already_owned"),
        (ImportedFileStatus.DUPLICATE_FILE, ImportSeriesStatus.MATCHED, "duplicate_file"),
        (ImportedFileStatus.NO_MATCH, ImportSeriesStatus.MATCHED, "no_match"),
        (ImportedFileStatus.PENDING, ImportSeriesStatus.NO_MATCH, "no_match"),
        (ImportedFileStatus.PENDING, ImportSeriesStatus.MATCHED, None),
    ],
)
def test_file_group_key(
    file_status: ImportedFileStatus,
    series_status: ImportSeriesStatus,
    expected: str | None,
) -> None:
    imp_file = ImportedFile(
        import_job_id=1,
        import_series_id=1,
        file_path="/source/Issue.cbz",
        file_name="Issue.cbz",
        file_format="cbz",
        status=file_status,
    )
    imported_series = ImportedSeries(
        import_job_id=1,
        raw_series_name="Series",
        status=series_status,
    )

    assert _import_review_file_group_key(imp_file, imported_series) == expected


def test_build_file_detail_row_shapes_duplicate_diagnostics() -> None:
    imp_file = ImportedFile(
        id=11,
        import_job_id=1,
        import_series_id=1,
        file_path="/source/Issue.cbz",
        file_name="Issue.cbz",
        file_size=123,
        file_format="cbz",
        has_comicinfo=True,
        status=ImportedFileStatus.DUPLICATE_FILE,
        match_confidence="high",
        diagnostics={
            "duplicate_reason": "hash_confirmed_duplicate",
            "representative_file_name": "Issue-alt.cbz",
        },
    )

    row = _build_import_review_file_detail_row(imp_file, {})

    assert row["id"] == 11
    assert row["status"] == "duplicate_file"
    assert row["duplicate_keep_label"] == "Issue-alt.cbz"
    assert "confirmed by hash" in str(row["duplicate_reason_label"])


@pytest.mark.asyncio
async def test_review_target_loaders_return_empty_without_visible_series(db_session) -> None:  # type: ignore[no-untyped-def]
    assert await _load_import_review_matched_file_targets(db_session, 1, []) == {}
    assert await _load_import_review_file_detail_groups(db_session, 1, []) == {}
