"""Regression coverage for canonical issue-number rendering."""

from __future__ import annotations

import pytest

from pullbox.core.issue_numbers import (
    format_issue_number,
    issue_number_text_matches_numeric,
    normalize_issue_number_text,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_000_000.0, "1000000"),
        (12.25, "12.25"),
        (0.5, "0.5"),
        (0.000001, "0.000001"),
        (1e86, "1" + ("0" * 86)),
        (-1.0, "-1"),
        (-0.5, "-0.5"),
    ],
)
def test_format_issue_number_is_exact_and_never_scientific(
    value: float,
    expected: str,
) -> None:
    assert format_issue_number(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("001", "1"),
        ("0.50", "0.5"),
        ("1au", "1AU"),
        ("½", "0.5"),
        ("1e6", "1000000"),
    ],
)
def test_normalize_issue_number_text_preserves_exact_semantics(
    value: str,
    expected: str,
) -> None:
    assert normalize_issue_number_text(value) == expected


@pytest.mark.parametrize("value", ["", "nan", "inf", "Annual 1"])
def test_normalize_issue_number_text_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="issue number"):
        normalize_issue_number_text(value)


def test_issue_number_text_numeric_compatibility_preserves_suffixes() -> None:
    assert issue_number_text_matches_numeric(1.0, "1AU") is True
    assert issue_number_text_matches_numeric(2.0, "1AU") is False
