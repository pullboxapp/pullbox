"""Canonical issue-number rendering shared across application boundaries."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

_MAX_ISSUE_NUMBER_TEXT_LENGTH = 320
_NUMERIC_SUFFIX_PATTERN = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))([A-Za-z]+)$")


def _format_decimal(value: Decimal) -> str:
    """Render a finite decimal without exponent or insignificant zeros."""
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def format_issue_number(value: float | int) -> str:
    """Render an issue number without scientific notation or trailing zeros."""
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if not decimal_value.is_finite():
        return str(value)
    return _format_decimal(decimal_value)


def _issue_number_parts(value: str | float | int) -> tuple[Decimal, str]:
    raw_value = str(value).strip()
    if not raw_value:
        raise ValueError("issue number must not be blank")

    normalized_fraction = raw_value.replace("½", ".5").replace("¼", ".25").replace("¾", ".75")
    normalized_fraction = re.sub(
        r"(?<=[0-9.])\s+(?=[A-Za-z]+$)",
        "",
        normalized_fraction,
    )
    suffix = ""
    try:
        numeric_value = Decimal(normalized_fraction)
    except InvalidOperation:
        match = _NUMERIC_SUFFIX_PATTERN.fullmatch(normalized_fraction)
        if match is None:
            raise ValueError(f"invalid issue number: {raw_value!r}") from None
        numeric_value = Decimal(match.group(1))
        suffix = match.group(2).upper()

    if not numeric_value.is_finite():
        raise ValueError(f"invalid issue number: {raw_value!r}")

    exact_text = f"{_format_decimal(numeric_value)}{suffix}"
    if len(exact_text) > _MAX_ISSUE_NUMBER_TEXT_LENGTH:
        raise ValueError("issue number exceeds the supported exact-text length")
    return numeric_value, exact_text


def normalize_issue_number_text(value: str | float | int) -> str:
    """Normalize an exact numeric issue designation while preserving a suffix."""
    return _issue_number_parts(value)[1]


def issue_number_text_matches_numeric(
    issue_number: float | int,
    issue_number_text: str,
) -> bool:
    """Return whether exact text has the same numeric compatibility value."""
    exact_numeric_value, _ = _issue_number_parts(issue_number_text)
    try:
        numeric_value = Decimal(str(issue_number))
    except InvalidOperation:
        return False
    return numeric_value.is_finite() and numeric_value == exact_numeric_value


def parse_issue_number_text(value: str | float | int) -> tuple[float, str]:
    """Return the numeric compatibility value and normalized exact designation."""
    numeric_value, exact_text = _issue_number_parts(value)
    float_value = float(numeric_value)
    if not math.isfinite(float_value):
        raise ValueError("issue number exceeds the numeric compatibility range")
    return float_value, exact_text
