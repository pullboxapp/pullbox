"""Release validation service for search results.

Orchestrates parsing + matching + validation for search results against
wanted issues. Uses the same NameMatcher and release parser as MatchingService
to ensure consistent behavior between disk-matching and download-matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from pullbox.core.release_parser import ParsedRelease
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.semantic_matching import (
    SearchPolicy,
    SemanticMatchEngine,
    build_semantic_config,
)

if TYPE_CHECKING:
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.search_types import SearchEvalKwargs, ValidatorKwargs

log = structlog.get_logger(__name__)

DEFAULT_IGNORE_WORDS = [
    "covers only",
    "cover only",
    "extras only",
    "extra pages only",
    "preview",
    "noir edition",
    "dc go edition",
    "sampler",
    "ashcan",
    "sketch",
    "virgin",
    "incentive",
    "poster",
    "print",
    "blank cover",
]

VALIDATOR_OPTION_KEYS = frozenset(
    {
        "ignore_words",
        "fuzzy_high_threshold",
        "fuzzy_low_threshold",
        "year_tolerance",
        "warn_issue_mb",
        "warn_collection_mb",
    }
)


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a release against a wanted issue."""

    is_match: bool
    confidence: MatchConfidence
    parsed: ParsedRelease
    release: ReleaseResult
    rejection_reason: str | None = None
    series_similarity: float = 0.0
    match_type: str = "none"
    issue_match: bool = False
    year_match: bool | None = None
    issue_type_match: bool = False
    size_warning: str | None = None


# Confidence sort order for descending sort
_CONFIDENCE_ORDER = {
    MatchConfidence.HIGH: 0,
    MatchConfidence.MEDIUM: 1,
    MatchConfidence.LOW: 2,
}

# Issue types treated as single-issue for size heuristic
_SINGLE_ISSUE_TYPES = frozenset(
    {IssueType.ISSUE, IssueType.ONE_SHOT, IssueType.SPECIAL, IssueType.ANNUAL}
)

# Non-standard types where release titles commonly lack issue numbers.
# Also used for size heuristic (flag if suspiciously small).
_COLLECTION_TYPES = frozenset(
    {
        IssueType.TPB,
        IssueType.OMNIBUS,
        IssueType.HC,
        IssueType.COMPENDIUM,
        IssueType.VOLUME,
        IssueType.GN,
        IssueType.OGN,
        IssueType.DELUXE,
        IssueType.ONE_SHOT,
    }
)

_MB = 1024 * 1024


def _lower_confidence(confidence: MatchConfidence) -> MatchConfidence:
    """Lower confidence by one tier. LOW stays LOW."""
    if confidence == MatchConfidence.HIGH:
        return MatchConfidence.MEDIUM
    return MatchConfidence.LOW


def _compile_ignore_pattern(word: str) -> re.Pattern[str]:
    """Compile ignore words so spaces also match dotted/dashed release names."""
    tokens = [re.escape(token) for token in word.lower().split()]
    pattern = r"[\W_]+".join(tokens) if len(tokens) > 1 else re.escape(word.lower())
    return re.compile(r"\b" + pattern + r"\b")


def _apply_size_heuristic(
    confidence: MatchConfidence,
    issue_type: IssueType,
    size_bytes: int | None,
    warn_issue_mb: int = 750,
    warn_collection_mb: int = 50,
) -> tuple[MatchConfidence, str | None]:
    """Apply file size heuristic to modify confidence.

    Never hard-rejects — only lowers confidence and adds warnings.

    Returns:
        Tuple of (adjusted_confidence, warning_message_or_none).
    """
    if size_bytes is None:
        return confidence, None

    size_mb = size_bytes / _MB

    # Single issues: flag if unexpectedly large
    # Lower confidence by one level and add a warning — never hard-floor to LOW.
    # Anniversary editions, graphic novels stored as STANDARD, and high-res
    # scans can legitimately be 500+ MB.
    if issue_type in _SINGLE_ISSUE_TYPES:
        if size_mb > warn_issue_mb * 1.5:  # > 1125MB default
            lower = _lower_confidence(confidence)
            return (
                lower,
                f"Unusually large for single issue ({size_mb:.0f} MB)",
            )
        if size_mb > warn_issue_mb:  # > 750MB default
            lower = _lower_confidence(confidence)
            return lower, f"Large for single issue ({size_mb:.0f} MB)"

    # Collections: flag if unexpectedly small
    if issue_type in _COLLECTION_TYPES and size_mb < warn_collection_mb:
        return (
            MatchConfidence.LOW,
            f"Small for collection ({size_mb:.0f} MB)",
        )

    return confidence, None


def validator_kwargs_from_eval_kwargs(eval_kwargs: SearchEvalKwargs) -> ValidatorKwargs:
    """Extract ReleaseValidator-compatible options from search evaluation kwargs."""
    kwargs: ValidatorKwargs = {}
    if "ignore_words" in eval_kwargs:
        kwargs["ignore_words"] = eval_kwargs["ignore_words"]
    if "fuzzy_high_threshold" in eval_kwargs:
        kwargs["fuzzy_high_threshold"] = eval_kwargs["fuzzy_high_threshold"]
    if "fuzzy_low_threshold" in eval_kwargs:
        kwargs["fuzzy_low_threshold"] = eval_kwargs["fuzzy_low_threshold"]
    if "year_tolerance" in eval_kwargs:
        kwargs["year_tolerance"] = eval_kwargs["year_tolerance"]
    if "warn_issue_mb" in eval_kwargs:
        kwargs["warn_issue_mb"] = eval_kwargs["warn_issue_mb"]
    if "warn_collection_mb" in eval_kwargs:
        kwargs["warn_collection_mb"] = eval_kwargs["warn_collection_mb"]
    return kwargs


class ReleaseValidator:
    """Validates search results against wanted issues.

    Uses the same NameMatcher and IssueType detection as MatchingService
    to ensure consistent behavior between disk-matching and download-matching.

    Args:
        ignore_words: Custom ignore words list. ``None`` uses defaults.
        fuzzy_high_threshold: High-confidence fuzzy cutoff (0.0-1.0).
            Defaults to 0.85.
        fuzzy_low_threshold: Low-confidence fuzzy cutoff (0.0-1.0).
            Defaults to 0.70.
        year_tolerance: Max year difference for year match (default 1).
    """

    def __init__(
        self,
        ignore_words: list[str] | None = None,
        *,
        fuzzy_high_threshold: float | None = None,
        fuzzy_low_threshold: float | None = None,
        year_tolerance: int | None = None,
        warn_issue_mb: int | None = None,
        warn_collection_mb: int | None = None,
    ) -> None:
        self._fuzzy_high = fuzzy_high_threshold
        self._fuzzy_low = fuzzy_low_threshold
        self._year_tolerance = year_tolerance if year_tolerance is not None else 1
        self._warn_issue_mb = warn_issue_mb if warn_issue_mb is not None else 750
        self._warn_collection_mb = warn_collection_mb if warn_collection_mb is not None else 50
        words = ignore_words if ignore_words is not None else DEFAULT_IGNORE_WORDS
        self._ignore_patterns = [_compile_ignore_pattern(w) for w in words]
        self._extractor = SourceMetadataExtractor()
        self._engine = SemanticMatchEngine(
            config=build_semantic_config(
                ignore_words=words,
                fuzzy_high_threshold=fuzzy_high_threshold,
                fuzzy_low_threshold=fuzzy_low_threshold,
                year_tolerance=year_tolerance,
                warn_issue_mb=warn_issue_mb,
                warn_collection_mb=warn_collection_mb,
            ),
            policy=SearchPolicy(),
        )

    def validate_results(
        self,
        results: list[ReleaseResult],
        wanted_series: str,
        wanted_issue: float,
        wanted_year: int | None = None,
        wanted_issue_type: IssueType = IssueType.ISSUE,
        alternate_names: list[str] | None = None,
    ) -> list[ValidationResult]:
        """Validate a list of search results against a wanted issue.

        Returns only validated (matched) results, sorted by confidence descending.
        Rejected results are logged at DEBUG level with rejection reasons.
        """
        vlog = log.bind(
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_type=str(wanted_issue_type),
        )
        vlog.debug("validation_start", total_results=len(results))

        validated: list[ValidationResult] = []
        reject_counts: dict[str, int] = {}

        for result in results:
            vr = self._validate_one(
                result,
                wanted_series=wanted_series,
                wanted_issue=wanted_issue,
                wanted_year=wanted_year,
                wanted_issue_type=wanted_issue_type,
                alternate_names=alternate_names,
            )
            if vr.is_match:
                validated.append(vr)
                vlog.debug(
                    "release_accepted",
                    title=result.title,
                    parsed_series=vr.parsed.series_name,
                    parsed_issue=vr.parsed.issue_number,
                    parsed_year=vr.parsed.year,
                    parsed_type=str(vr.parsed.issue_type),
                    confidence=str(vr.confidence),
                    similarity=round(vr.series_similarity, 2),
                )
            else:
                # Categorize rejection for summary
                reason = vr.rejection_reason or "unknown"
                category = reason.split(":")[0].strip()
                reject_counts[category] = reject_counts.get(category, 0) + 1
                vlog.debug(
                    "release_rejected",
                    title=result.title,
                    reason=vr.rejection_reason,
                    parsed_series=vr.parsed.series_name,
                    parsed_issue=vr.parsed.issue_number,
                    parsed_type=str(vr.parsed.issue_type),
                )

        # Sort by confidence (HIGH first)
        validated.sort(key=lambda v: _CONFIDENCE_ORDER.get(v.confidence, 99))

        vlog.info(
            "validation_complete",
            total=len(results),
            accepted=len(validated),
            rejected=len(results) - len(validated),
            reject_reasons=reject_counts,
            confidence_breakdown={
                "high": sum(1 for v in validated if v.confidence == MatchConfidence.HIGH),
                "medium": sum(1 for v in validated if v.confidence == MatchConfidence.MEDIUM),
                "low": sum(1 for v in validated if v.confidence == MatchConfidence.LOW),
            },
        )
        return validated

    def validate_all_results(
        self,
        results: list[ReleaseResult],
        wanted_series: str,
        wanted_issue: float,
        wanted_year: int | None = None,
        wanted_issue_type: IssueType = IssueType.ISSUE,
        alternate_names: list[str] | None = None,
    ) -> tuple[list[ValidationResult], list[ValidationResult]]:
        """Validate results, returning (matched, rejected) tuples.

        Unlike ``validate_results`` which only returns matched results, this
        method preserves rejected results with their rejection reasons so the
        UI can display them for interactive search.

        Returns:
            A tuple of (matched, rejected) ``ValidationResult`` lists.
            Matched list is sorted by confidence descending (HIGH first).
        """
        vlog = log.bind(
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_type=str(wanted_issue_type),
        )
        vlog.debug("validate_all_start", total_results=len(results))

        matched: list[ValidationResult] = []
        rejected: list[ValidationResult] = []

        for result in results:
            vr = self._validate_one(
                result,
                wanted_series=wanted_series,
                wanted_issue=wanted_issue,
                wanted_year=wanted_year,
                wanted_issue_type=wanted_issue_type,
                alternate_names=alternate_names,
            )
            if vr.is_match:
                matched.append(vr)
            else:
                rejected.append(vr)

        # Sort matched by confidence (HIGH first)
        matched.sort(key=lambda v: _CONFIDENCE_ORDER.get(v.confidence, 99))

        vlog.debug(
            "validate_all_complete",
            total=len(results),
            matched=len(matched),
            rejected=len(rejected),
        )
        return matched, rejected

    def _validate_one(
        self,
        result: ReleaseResult,
        *,
        wanted_series: str,
        wanted_issue: float,
        wanted_year: int | None,
        wanted_issue_type: IssueType,
        alternate_names: list[str] | None,
    ) -> ValidationResult:
        """Run the validation pipeline on a single result."""
        metadata = self._extractor.from_release_title(result.title)
        parsed = metadata.parsed_release
        if parsed is None:
            return self._reject(result, "Failed to parse release title")

        # Step 1½a: Reject non-comic content based on title tags
        # Titles explicitly tagged as non-comic media should be rejected before
        # any further matching — e.g. "[Audiobook]", "(Audiobook)", "[audio]"
        _non_comic_tags = re.compile(
            r"(?:\b|(?<=[\[\(]))(?:audiobook|audio\s*book|audio(?=[\]\)])|podcast|soundtrack|ost"
            r"|mp3|flac|aac|m4b|ogg"
            r"|tv\s*series|tv\s*show|documentary"
            r"|movie|film|dvdrip|bdrip|bluray|x264|x265|720p|1080p|2160p|4k)(?:\b|(?=[\]\)]))",
            re.IGNORECASE,
        )
        tag_match = _non_comic_tags.search(parsed.original_title)
        if tag_match:
            return self._reject(
                result,
                f"Non-comic content: '{tag_match.group()}' in title",
                parsed=parsed,
            )

        # Step 1½b: Reject based on indexer category
        # Categories can be numeric codes (7030) or name strings (Books/Comics).
        # Accept: 7xxx codes, or names containing "book", "comic", "ebook"
        # Reject: categories clearly indicating non-comic content (audio, TV, movie)
        if result.category:
            cat = str(result.category)
            cat_lower = cat.lower()

            # Name-based categories (from Prowlarr-proxied indexers)
            _reject_cat_names = re.compile(
                r"\b(?:audio|music|tv|movie|video|porn|xxx|game|software|app)\b",
                re.IGNORECASE,
            )
            _accept_cat_names = re.compile(
                r"\b(?:book|comic|ebook|e-book|manga|graphic\s*novel)\b",
                re.IGNORECASE,
            )

            if _accept_cat_names.search(cat_lower):
                pass  # explicitly comic/book — accept
            elif _reject_cat_names.search(cat_lower):
                return self._reject(
                    result,
                    f"Non-comic category: {cat}",
                    parsed=parsed,
                )
            else:
                # Numeric code check
                cat_codes = [c.strip() for c in cat.split(",")]
                has_comic_cat = any(c.startswith("70") for c in cat_codes if c.isdigit())
                has_any_numeric = any(c.isdigit() for c in cat_codes)
                if has_any_numeric and not has_comic_cat:
                    return self._reject(
                        result,
                        f"Non-comic category: {cat}",
                        parsed=parsed,
                    )

        if parsed.is_pack and wanted_issue_type in _SINGLE_ISSUE_TYPES:
            return self._reject(
                result,
                f"Multi-issue pack is not valid for single issue target: {parsed.pack_range}",
                parsed=parsed,
            )

        # Step 2: Ignore word check (phrase matching via word boundaries)
        # Skip ignore words that appear in the wanted series title itself —
        # e.g. don't reject "ashcan" releases when searching for an ashcan series.
        wanted_lower = wanted_series.lower()
        title_lower = parsed.original_title.lower()
        for pattern in self._ignore_patterns:
            if pattern.search(title_lower) and not pattern.search(wanted_lower):
                return self._reject(
                    result,
                    f"Contains ignore word: {pattern.pattern}",
                    parsed=parsed,
                )

        decision = self._engine.match_against_issue(
            metadata=metadata,
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_issue_type=wanted_issue_type,
            alternate_names=alternate_names,
        )
        if not decision.is_match:
            return self._reject(
                result,
                decision.rejection_reason or "Semantic match rejected",
                parsed=parsed,
            )

        year_matched: bool | None = None
        if parsed.year is not None and wanted_year is not None:
            year_matched = abs(parsed.year - wanted_year) <= self._year_tolerance

        # Step 8: File size heuristic (modifies confidence, never rejects)
        confidence, size_warning = _apply_size_heuristic(
            decision.confidence,
            wanted_issue_type,
            result.size_bytes,
            warn_issue_mb=self._warn_issue_mb,
            warn_collection_mb=self._warn_collection_mb,
        )

        return ValidationResult(
            is_match=True,
            confidence=confidence,
            parsed=parsed,
            release=result,
            rejection_reason=None,
            series_similarity=float(decision.match_diagnostics.get("series_similarity", 0.0)),
            match_type=str(decision.match_diagnostics.get("match_type", "none")),
            issue_match=True,
            year_match=year_matched,
            issue_type_match=True,
            size_warning=size_warning,
        )

    @staticmethod
    def _compute_confidence(
        *,
        match_type: str,
        similarity: float,
        year_match: bool | None,
        fuzzy_high_threshold: float = 0.85,
    ) -> MatchConfidence:
        """Determine confidence level from match quality signals.

        Args:
            fuzzy_high_threshold: Similarity cutoff above which a fuzzy match
                gets MEDIUM confidence (with year) instead of LOW.
        """
        # Exact or alternate name match
        if match_type in ("exact", "alternate"):
            if year_match is True:
                return MatchConfidence.HIGH
            if year_match is None:
                return MatchConfidence.MEDIUM
            # year_match is False (wrong year)
            return MatchConfidence.LOW

        # Starts-with match (target appears at start of parsed name + subtitle)
        # High quality — almost as good as exact, just has extra subtitle words
        if match_type == "starts_with":
            if year_match is True:
                return MatchConfidence.HIGH
            if year_match is None:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW

        # Token-subset match (all target tokens found in parsed name)
        if match_type in ("token_set", "token_subset"):
            if year_match is True:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW

        # Fuzzy match (≥ high threshold)
        if similarity >= fuzzy_high_threshold:
            if year_match is True:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW

        # Fuzzy match (low threshold - high threshold)
        return MatchConfidence.LOW

    @staticmethod
    def _reject(
        result: ReleaseResult,
        reason: str,
        parsed: ParsedRelease | None = None,
    ) -> ValidationResult:
        """Create a rejection ValidationResult."""
        if parsed is None:
            parsed = ParsedRelease(
                original_title=result.title,
                series_name=None,
                issue_number=None,
                year=None,
                volume=None,
                issue_type=IssueType.ISSUE,
                scan_group=None,
                file_format=None,
                is_pack=False,
                pack_range=None,
            )
        return ValidationResult(
            is_match=False,
            confidence=MatchConfidence.LOW,
            parsed=parsed,
            release=result,
            rejection_reason=reason,
        )
