"""Tests for conservative story-arc filename order evidence."""

from __future__ import annotations

import pytest

from pullbox.core.story_arc_ordering import extract_story_arc_order_prefix


@pytest.mark.parametrize(
    ("file_name", "reading_order", "raw", "residual"),
    [
        ("001 - Avengers 001.cbz", 1, "001", "Avengers 001.cbz"),
        ("002-X-Men 001.cbz", 2, "002", "X-Men 001.cbz"),
        ("10 - Event Finale.cbz", 10, "10", "Event Finale.cbz"),
    ],
)
def test_extract_story_arc_order_prefix_preserves_exact_evidence(
    file_name: str,
    reading_order: int,
    raw: str,
    residual: str,
) -> None:
    evidence = extract_story_arc_order_prefix(file_name)

    assert evidence is not None
    assert evidence.reading_order == reading_order
    assert evidence.reading_order_raw == raw
    assert evidence.residual_file_name == residual
    assert evidence.sort_key == (reading_order, raw, residual.casefold(), residual)


def test_non_padded_orders_sort_numerically() -> None:
    evidence = [
        extract_story_arc_order_prefix(name) for name in ("10 - C.cbz", "2 - B.cbz", "1 - A.cbz")
    ]

    assert all(item is not None for item in evidence)
    assert [item.reading_order for item in sorted(evidence, key=lambda item: item.sort_key)] == [
        1,
        2,
        10,
    ]


@pytest.mark.parametrize(
    "file_name",
    [
        "Avengers 001 - Finale.cbz",
        "001.cbz",
        "001-   ",
        "-001 Avengers.cbz",
        "000 - Avengers 001.cbz",
        "1234567890 - Avengers 001.cbz",
    ],
)
def test_unanchored_or_unsafe_prefixes_are_not_evidence(file_name: str) -> None:
    assert extract_story_arc_order_prefix(file_name) is None
