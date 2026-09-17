"""Inline review details describe current outcomes, including rechecked sources."""

from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.ui.import_review_tables import _build_import_review_file_detail_row


def test_rechecked_file_displays_format_from_source_evidence():
    file = ImportedFile(
        file_name="Batman.cbr",
        status=ImportedFileStatus.MATCHED,
        diagnostics={
            "source_metadata": {
                "archive_format": {"declared": "cbr", "detected": "cbz", "mismatch": True},
                "content_inspection": {"page_count": 24},
            }
        },
    )
    row = _build_import_review_file_detail_row(file, {})
    assert row["archive_format"] == {"declared": "cbr", "detected": "cbz", "mismatch": True}
    assert row["page_count"] == 24


def test_ready_file_is_not_labeled_as_an_excluded_duplicate():
    file = ImportedFile(file_name="Batman.cbz", status=ImportedFileStatus.MATCHED)
    row = _build_import_review_file_detail_row(file, {})
    assert row["duplicate_reason_label"] is None


def test_actual_duplicate_keeps_its_explanation():
    file = ImportedFile(file_name="Batman.cbz", status=ImportedFileStatus.DUPLICATE_FILE)
    row = _build_import_review_file_detail_row(file, {})
    assert "Excluded because" in row["duplicate_reason_label"]
