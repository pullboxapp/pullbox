"""Boundary tests for release-validator configuration and classification."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.release_validator import (
    ReleaseValidator,
    _compile_ignore_pattern,
    validator_kwargs_from_eval_kwargs,
)
from tests.conftest import make_release

if TYPE_CHECKING:
    from pullbox.services.search_types import SearchEvalKwargs


def test_validator_kwargs_extracts_every_supported_option() -> None:
    source = cast(
        "SearchEvalKwargs",
        {
            "ignore_words": ["sample"],
            "fuzzy_high_threshold": 0.9,
            "fuzzy_low_threshold": 0.6,
            "year_tolerance": 2,
            "warn_issue_mb": 900,
            "warn_collection_mb": 25,
            "unrelated": "ignored",
        },
    )

    assert validator_kwargs_from_eval_kwargs(source) == {
        "ignore_words": ["sample"],
        "fuzzy_high_threshold": 0.9,
        "fuzzy_low_threshold": 0.6,
        "year_tolerance": 2,
        "warn_issue_mb": 900,
        "warn_collection_mb": 25,
    }
    assert validator_kwargs_from_eval_kwargs(cast("SearchEvalKwargs", {})) == {}


def test_ignore_pattern_accepts_release_separators() -> None:
    pattern = _compile_ignore_pattern("covers only")

    assert pattern.search("Batman.COVERS-ONLY.cbz".lower())
    assert _compile_ignore_pattern("sampler").search("Batman sampler")


@pytest.mark.parametrize(
    ("match_type", "similarity", "year_match", "expected"),
    [
        ("exact", 1.0, True, MatchConfidence.HIGH),
        ("alternate", 1.0, None, MatchConfidence.MEDIUM),
        ("exact", 1.0, False, MatchConfidence.LOW),
        ("starts_with", 1.0, True, MatchConfidence.HIGH),
        ("starts_with", 1.0, None, MatchConfidence.MEDIUM),
        ("starts_with", 1.0, False, MatchConfidence.LOW),
        ("token_set", 0.9, True, MatchConfidence.MEDIUM),
        ("token_subset", 0.9, False, MatchConfidence.LOW),
        ("fuzzy", 0.9, True, MatchConfidence.MEDIUM),
        ("fuzzy", 0.9, None, MatchConfidence.LOW),
        ("fuzzy", 0.7, True, MatchConfidence.LOW),
    ],
)
def test_compute_confidence_covers_each_quality_boundary(
    match_type: str,
    similarity: float,
    year_match: bool | None,
    expected: MatchConfidence,
) -> None:
    assert (
        ReleaseValidator._compute_confidence(
            match_type=match_type,
            similarity=similarity,
            year_match=year_match,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("category", "reason_fragment"),
    [
        ("Audio", "Non-comic category"),
        ("5000, 5040", "Non-comic category"),
    ],
)
def test_validate_all_results_preserves_category_rejections(
    category: str,
    reason_fragment: str,
) -> None:
    validator = ReleaseValidator()
    release = replace(make_release("Batman 005 [2024] [Digital]"), category=category)

    matched, rejected = validator.validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=5,
        wanted_year=2024,
    )

    assert matched == []
    assert reason_fragment in (rejected[0].rejection_reason or "")


def test_book_category_remains_eligible_for_matching() -> None:
    validator = ReleaseValidator()
    release = replace(
        make_release("Batman 005 [2024] [Digital]"),
        category="Books/Comics",
    )

    matched = validator.validate_results(
        [release],
        wanted_series="Batman",
        wanted_issue=5,
        wanted_year=2024,
    )

    assert len(matched) == 1


def test_non_comic_title_tag_is_rejected_before_semantic_matching() -> None:
    validator = ReleaseValidator()

    matched, rejected = validator.validate_all_results(
        [make_release("Batman 005 [2024] [Audiobook]")],
        wanted_series="Batman",
        wanted_issue=5,
        wanted_year=2024,
        wanted_issue_type=IssueType.ISSUE,
    )

    assert matched == []
    assert "Non-comic content" in (rejected[0].rejection_reason or "")


def test_reject_builds_safe_parse_fallback() -> None:
    release = make_release("unparseable")

    result = ReleaseValidator._reject(release, "No parse")

    assert result.is_match is False
    assert result.parsed.original_title == "unparseable"
    assert result.parsed.series_name is None
    assert result.rejection_reason == "No parse"
