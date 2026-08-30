"""Regression coverage for canonical issue-number rendering."""

from __future__ import annotations

import pytest

from pullbox.core.issue_numbers import format_issue_number


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_000_000.0, "1000000"),
        (12.25, "12.25"),
        (0.5, "0.5"),
        (0.000001, "0.000001"),
        (-1.0, "-1"),
        (-0.5, "-0.5"),
    ],
)
def test_format_issue_number_is_exact_and_never_scientific(
    value: float,
    expected: str,
) -> None:
    assert format_issue_number(value) == expected
