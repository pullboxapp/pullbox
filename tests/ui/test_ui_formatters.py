"""Tests for shared UI formatter helpers."""

from __future__ import annotations

from pullbox.models.series import SeriesStatus
from pullbox.ui.formatters import (
    format_eta,
    format_filesize,
    format_issue_number,
    format_series_year_label,
    format_type_display,
    humanize_download_error,
)


def test_format_filesize_uses_existing_compact_units() -> None:
    assert format_filesize(512) == "512 B"
    assert format_filesize(1536) == "1.5 KB"
    assert format_filesize(2 * 1024 * 1024) == "2.0 MB"


def test_format_eta_uses_existing_short_duration_copy() -> None:
    assert format_eta(None) == ""
    assert format_eta(59) == "59s"
    assert format_eta(65) == "1m 5s"
    assert format_eta(3660) == "1h 1m"


def test_format_issue_number_never_uses_scientific_notation() -> None:
    assert format_issue_number(1_000_000.0) == "1000000"
    assert format_issue_number(12.25) == "12.25"


def test_format_series_year_label_preserves_status_semantics() -> None:
    assert format_series_year_label(2025, None, SeriesStatus.CONTINUING) == "2025\u2013present"
    assert format_series_year_label(2020, 2023, SeriesStatus.ENDED) == "2020\u20132023"
    assert format_series_year_label(None, None, SeriesStatus.UNKNOWN) == "Unknown"


def test_type_display_and_error_hints_match_template_contracts() -> None:
    assert format_type_display("tpb") == "Trade Paperback"
    assert format_type_display("weird_format") == "Weird Format"
    assert humanize_download_error("connection refused")["label"] == "Connection refused"
