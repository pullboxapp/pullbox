"""Boundary coverage for semantic matching normalization and scoring."""

from __future__ import annotations

from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.providers.base import SeriesSearchResult
from pullbox.services.semantic_matching import (
    SearchPolicy,
    SemanticMatchEngine,
    _allows_implicit_issue_one,
    _as_float,
    _as_int,
    _is_safe_single_word_collection_prefix,
    _normalize_volume_number,
    semantic_config_from_eval_kwargs,
)


def test_numeric_matching_configuration_coercion_is_bounded() -> None:
    assert [_as_float(value) for value in (None, True, 2, 2.5, "3.5", "bad", object())] == [
        None,
        1.0,
        2.0,
        2.5,
        3.5,
        None,
        None,
    ]
    assert [_as_int(value) for value in (None, True, 2, 2.5, "3", "bad", object())] == [
        None,
        1,
        2,
        2,
        3,
        None,
        None,
    ]
    config = semantic_config_from_eval_kwargs(
        {
            "ignore_words": "not-a-list",
            "fuzzy_high_threshold": "0.9",
            "year_tolerance": "2",
        }
    )
    assert config.ignore_words == []
    assert config.fuzzy_high_threshold == 0.9
    assert config.year_tolerance == 2


def test_volume_number_normalization_accepts_tokens_and_rejects_noise() -> None:
    assert _normalize_volume_number("2") == 2.0
    assert _normalize_volume_number("Volume 03.5 Deluxe") == 3.5
    assert _normalize_volume_number("Deluxe") is None


def test_semantic_confidence_boundaries_are_explicit() -> None:
    engine = SemanticMatchEngine()
    assert (
        engine._compute_confidence(match_type="starts_with", similarity=1.0, year_match=None)
        == MatchConfidence.MEDIUM
    )
    assert (
        engine._compute_confidence(match_type="starts_with", similarity=1.0, year_match=False)
        == MatchConfidence.LOW
    )
    assert (
        engine._compute_confidence(match_type="token_set", similarity=1.0, year_match=True)
        == MatchConfidence.MEDIUM
    )
    assert (
        engine._compute_confidence(match_type="fuzzy", similarity=0.99, year_match=False)
        == MatchConfidence.LOW
    )


def test_series_shape_scoring_rewards_credible_size_and_penalizes_bad_shape() -> None:
    engine = SemanticMatchEngine()
    collection = SourceMetadata(
        original_title="Example Vol 2",
        series_name="Example",
        issue_number=2.0,
        issue_type=IssueType.TPB,
        issue_count_hint=2,
    )
    compact = SeriesSearchResult("1", "Example", 2026, None, 2, "ended", None, None)
    oversized = SeriesSearchResult("2", "Example", 2026, None, 12, "continuing", None, None)
    assert engine._series_shape_adjustment(collection, compact) > 0
    assert engine._series_shape_adjustment(collection, oversized) < 0

    standard = collection.model_copy(
        update={"issue_type": IssueType.ISSUE, "issue_count_hint": None, "issue_number": None}
    )
    singleton = SeriesSearchResult("3", "Example", 2026, None, 1, "complete", None, None)
    short = SeriesSearchResult("4", "Example", 2026, None, 3, "finished", None, None)
    assert engine._series_shape_adjustment(standard, singleton) < 0
    assert engine._series_shape_adjustment(standard, short) < 0


def test_single_word_collection_prefix_requires_exact_safe_shape() -> None:
    metadata = SourceMetadata(
        original_title="Taproot A Story About A Gardener",
        series_name="Taproot A Story About A Gardener",
        year=2026,
        issue_type=IssueType.TPB,
        signals={"issue_type": MetadataSignal.RELEASE_TITLE},
    )
    assert _is_safe_single_word_collection_prefix(
        policy=SearchPolicy(),
        metadata=metadata,
        wanted_series="Taproot",
        wanted_issue=1.0,
        wanted_year=2026,
        wanted_issue_type=IssueType.TPB,
        wanted_series_issue_count=1,
    )
    assert not _is_safe_single_word_collection_prefix(
        policy=SearchPolicy(),
        metadata=metadata,
        wanted_series="Taproot Deluxe",
        wanted_issue=1.0,
        wanted_year=2026,
        wanted_issue_type=IssueType.TPB,
        wanted_series_issue_count=1,
    )
    assert not _is_safe_single_word_collection_prefix(
        policy=SearchPolicy(),
        metadata=metadata.model_copy(update={"series_name": "Elsewhere Deluxe Edition"}),
        wanted_series="Taproot",
        wanted_issue=1.0,
        wanted_year=2026,
        wanted_issue_type=IssueType.TPB,
        wanted_series_issue_count=1,
    )


def test_implicit_issue_one_rejects_collection_types() -> None:
    metadata = SourceMetadata(original_title="Example", issue_type=IssueType.TPB)
    assert not _allows_implicit_issue_one(metadata)
