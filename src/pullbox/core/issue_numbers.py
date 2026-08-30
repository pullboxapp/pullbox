"""Canonical issue-number rendering shared across application boundaries."""

from __future__ import annotations

import math
from decimal import Decimal


def format_issue_number(value: float | int) -> str:
    """Render an issue number without scientific notation or trailing zeros."""
    if isinstance(value, int):
        return str(value)
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        return str(value)
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
