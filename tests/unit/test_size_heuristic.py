"""Tests for file size heuristic confidence modifier — ESM Phase 3, Task 3.4.

Covers:
- Normal-sized issue: confidence unchanged
- Large issue (>750MB): confidence lowered one tier
- Very large issue (>1125MB): confidence lowered one tier
- Small collection (<50MB): confidence set to LOW with warning
- Normal collection: no change
- None size: no change
- Warning message describes the size concern
- Custom thresholds respected
- ONE_SHOT type treated as single issue

Run:
    pytest tests/unit/test_size_heuristic.py -v
"""

from __future__ import annotations

from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.release_validator import _apply_size_heuristic

_MB = 1024 * 1024


class TestSizeHeuristic:
    """Tests for _apply_size_heuristic() confidence modifier."""

    def test_normal_issue_size_no_change(self) -> None:
        """100MB issue: confidence unchanged."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ISSUE, 100 * _MB)
        assert conf == MatchConfidence.HIGH
        assert warning is None

    def test_large_issue_lowers_confidence(self) -> None:
        """800MB issue: confidence lowered one tier (HIGH→MEDIUM)."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ISSUE, 800 * _MB)
        assert conf == MatchConfidence.MEDIUM
        assert warning is not None
        assert "800" in warning

    def test_large_issue_medium_to_low(self) -> None:
        """800MB issue with MEDIUM confidence: lowered to LOW."""
        conf, warning = _apply_size_heuristic(MatchConfidence.MEDIUM, IssueType.ISSUE, 800 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None

    def test_very_large_issue_lowers_one_level(self) -> None:
        """1200MB+ issue (>1.5x threshold): confidence lowered one level."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ISSUE, 1200 * _MB)
        assert conf == MatchConfidence.MEDIUM
        assert warning is not None
        assert "1200" in warning

    def test_very_large_issue_medium_to_low(self) -> None:
        """1200MB+ issue with MEDIUM: lowered to LOW."""
        conf, _warning = _apply_size_heuristic(MatchConfidence.MEDIUM, IssueType.ISSUE, 1200 * _MB)
        assert conf == MatchConfidence.LOW

    def test_small_collection_flagged(self) -> None:
        """30MB TPB: flagged as suspicious, confidence set to LOW."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.TPB, 30 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None
        assert "30" in warning

    def test_normal_collection_no_flag(self) -> None:
        """200MB TPB: no flag, confidence unchanged."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.TPB, 200 * _MB)
        assert conf == MatchConfidence.HIGH
        assert warning is None

    def test_none_size_no_change(self) -> None:
        """Missing file size (None): confidence unchanged."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ISSUE, None)
        assert conf == MatchConfidence.HIGH
        assert warning is None

    def test_warning_message_included(self) -> None:
        """Warning message describes the size concern."""
        _, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ISSUE, 800 * _MB)
        assert warning is not None
        assert "large" in warning.lower() or "issue" in warning.lower()
        assert "800" in warning

    def test_small_collection_hc(self) -> None:
        """30MB HC: flagged as suspicious like TPB."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.HC, 30 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None

    def test_small_collection_omnibus(self) -> None:
        """30MB OMNIBUS: flagged as suspicious."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.OMNIBUS, 30 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None

    def test_small_collection_compendium(self) -> None:
        """30MB COMPENDIUM: flagged as suspicious."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.COMPENDIUM, 30 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None

    def test_custom_thresholds(self) -> None:
        """Custom thresholds are respected."""
        # 150MB issue with custom warn at 100MB → should trigger
        conf, warning = _apply_size_heuristic(
            MatchConfidence.HIGH,
            IssueType.ISSUE,
            150 * _MB,
            warn_issue_mb=100,
        )
        assert conf == MatchConfidence.MEDIUM
        assert warning is not None

    def test_custom_collection_threshold(self) -> None:
        """Custom collection threshold is respected."""
        # 80MB TPB with custom warn at 100MB → should trigger
        conf, warning = _apply_size_heuristic(
            MatchConfidence.HIGH,
            IssueType.TPB,
            80 * _MB,
            warn_collection_mb=100,
        )
        assert conf == MatchConfidence.LOW
        assert warning is not None

    def test_one_shot_treated_as_issue(self) -> None:
        """ONE_SHOT uses issue size thresholds (single issue)."""
        conf, warning = _apply_size_heuristic(MatchConfidence.HIGH, IssueType.ONE_SHOT, 800 * _MB)
        assert conf == MatchConfidence.MEDIUM
        assert warning is not None

    def test_low_confidence_stays_low(self) -> None:
        """LOW confidence is not lowered further (stays LOW)."""
        conf, warning = _apply_size_heuristic(MatchConfidence.LOW, IssueType.ISSUE, 800 * _MB)
        assert conf == MatchConfidence.LOW
        assert warning is not None


class TestSizeHeuristicIntegration:
    """Tests that size heuristic is applied in the validation pipeline."""

    def test_validator_applies_size_heuristic(self) -> None:
        """ValidationResult includes size_warning when heuristic triggers."""
        from pullbox.providers.base import ReleaseResult
        from pullbox.services.release_validator import ReleaseValidator

        release = ReleaseResult(
            title="Batman 001 (2016).cbz",
            indexer_name="NZBgeek",
            download_url="https://example.com/dl",
            size_bytes=800 * _MB,
            age_days=2,
            seeders=None,
            leechers=None,
            grabs=None,
            is_torrent=False,
            category=None,
            published_at=None,
        )
        validator = ReleaseValidator()
        results = validator.validate_results(
            [release],
            wanted_series="Batman",
            wanted_issue=1.0,
            wanted_year=2016,
            wanted_issue_type=IssueType.ISSUE,
        )
        assert len(results) == 1
        assert results[0].confidence == MatchConfidence.MEDIUM
        assert results[0].size_warning is not None
        assert "800" in results[0].size_warning

    def test_validator_no_warning_normal_size(self) -> None:
        """Normal-sized release has no size_warning."""
        from pullbox.providers.base import ReleaseResult
        from pullbox.services.release_validator import ReleaseValidator

        release = ReleaseResult(
            title="Batman 001 (2016).cbz",
            indexer_name="NZBgeek",
            download_url="https://example.com/dl",
            size_bytes=100 * _MB,
            age_days=2,
            seeders=None,
            leechers=None,
            grabs=None,
            is_torrent=False,
            category=None,
            published_at=None,
        )
        validator = ReleaseValidator()
        results = validator.validate_results(
            [release],
            wanted_series="Batman",
            wanted_issue=1.0,
            wanted_year=2016,
            wanted_issue_type=IssueType.ISSUE,
        )
        assert len(results) == 1
        assert results[0].size_warning is None
