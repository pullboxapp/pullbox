"""Shared semantic matching engine used by search, library matching, and import."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pullbox.core.issue_title import collection_title_number, collection_title_subtitle
from pullbox.core.name_matcher import NameMatcher, NameMatchResult
from pullbox.core.naming import extract_base_series_title
from pullbox.core.release_parser import issues_match, normalize_issue_number
from pullbox.core.release_year_matching import ReleaseYearContext, match_release_year
from pullbox.core.source_metadata import MetadataSignal
from pullbox.core.type_semantics import (
    TypeFamily,
    issue_type_compatibility,
    issue_type_family,
)
from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.import_embedded_title_match import embedded_issue_number_title_match

if TYPE_CHECKING:
    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.providers.base import SeriesSearchResult
    from pullbox.services.search_types import SearchEvalKwargs

_matcher = NameMatcher()
_PART_TITLE_RE = re.compile(
    r"\bpart\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_COLLECTION_ORDINAL_RE = re.compile(
    r"\b(?:book|v|vol(?:ume)?)\.?\s*#?0*(?P<number>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def _allows_implicit_issue_one(metadata: SourceMetadata) -> bool:
    """Return True when an issue-one release can safely omit an explicit issue number."""
    if metadata.issue_type not in {
        IssueType.ISSUE,
        IssueType.ANNUAL,
        IssueType.ONE_SHOT,
        IssueType.SPECIAL,
    }:
        return False
    return not _PART_TITLE_RE.search(metadata.original_title)


@dataclass(frozen=True, slots=True)
class SemanticMatchConfig:
    """Semantic matching knobs shared across workflows."""

    ignore_words: list[str] = field(default_factory=list)
    fuzzy_high_threshold: float = NameMatcher.DEFAULT_FUZZY_HIGH
    fuzzy_low_threshold: float = NameMatcher.DEFAULT_FUZZY_LOW
    year_tolerance: int = 1
    warn_issue_mb: int = 750
    warn_collection_mb: int = 50


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    """Policy toggles that differentiate workflows while sharing semantics."""

    name: str
    allow_collection_issue_match_fallback: bool
    allow_standard_collection_inference: bool
    allow_issue_like_standard_compatibility: bool
    allow_issue_like_issue_inference: bool
    require_volume_subtitle_corroboration: bool


@dataclass(frozen=True, slots=True)
class SearchPolicy(WorkflowPolicy):
    """Permissive policy used for search result evaluation."""

    def __init__(self) -> None:
        object.__setattr__(self, "name", "search")
        object.__setattr__(self, "allow_collection_issue_match_fallback", False)
        object.__setattr__(self, "allow_standard_collection_inference", True)
        object.__setattr__(self, "allow_issue_like_standard_compatibility", True)
        object.__setattr__(self, "allow_issue_like_issue_inference", True)
        object.__setattr__(self, "require_volume_subtitle_corroboration", False)


@dataclass(frozen=True, slots=True)
class LibraryMatchPolicy(WorkflowPolicy):
    """Policy used for local library file auto-matching."""

    def __init__(self) -> None:
        object.__setattr__(self, "name", "library")
        object.__setattr__(self, "allow_collection_issue_match_fallback", False)
        object.__setattr__(self, "allow_standard_collection_inference", True)
        object.__setattr__(self, "allow_issue_like_standard_compatibility", True)
        object.__setattr__(self, "allow_issue_like_issue_inference", True)
        object.__setattr__(self, "require_volume_subtitle_corroboration", True)


@dataclass(frozen=True, slots=True)
class ImportPolicy(WorkflowPolicy):
    """Stricter policy used for collection import review and matching."""

    def __init__(self) -> None:
        object.__setattr__(self, "name", "import")
        object.__setattr__(self, "allow_collection_issue_match_fallback", False)
        object.__setattr__(self, "allow_standard_collection_inference", False)
        object.__setattr__(self, "allow_issue_like_standard_compatibility", True)
        object.__setattr__(self, "allow_issue_like_issue_inference", False)
        object.__setattr__(self, "require_volume_subtitle_corroboration", True)


@dataclass(frozen=True, slots=True)
class IssueMatchDecision:
    """Structured issue-match decision from the semantic engine."""

    is_match: bool
    confidence: MatchConfidence
    match_method: str
    rejection_reason: str | None = None
    lowers_confidence: bool = False
    match_diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SeriesMatchDecision:
    """Structured series-candidate scoring result."""

    score: float
    compatible_type: bool
    match_method: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class SemanticMatchEngine:
    """Shared semantic engine for matching parsed source metadata to targets."""

    def __init__(
        self,
        config: SemanticMatchConfig | None = None,
        policy: WorkflowPolicy | None = None,
    ) -> None:
        self._config = config or SemanticMatchConfig()
        self._policy = policy or SearchPolicy()

    def match_against_issue(
        self,
        *,
        metadata: SourceMetadata,
        wanted_series: str,
        wanted_issue: float,
        wanted_year: int | None = None,
        wanted_issue_type: IssueType = IssueType.ISSUE,
        alternate_names: list[str] | None = None,
        wanted_issue_cv_id: int | None = None,
        wanted_issue_title: str | None = None,
        wanted_series_issue_count: int | None = None,
        year_context: ReleaseYearContext | None = None,
    ) -> IssueMatchDecision:
        """Return a workflow-aware semantic match decision for one issue target."""
        series_name = metadata.series_name or ""
        match_result = _matcher.match(
            series_name,
            wanted_series,
            alternate_names,
            fuzzy_high_threshold=self._config.fuzzy_high_threshold,
            fuzzy_low_threshold=self._config.fuzzy_low_threshold,
        )
        stripped_wanted = extract_base_series_title(wanted_series)
        if stripped_wanted != wanted_series and (
            issue_type_family(metadata.issue_type) == TypeFamily.COLLECTION
            or wanted_issue_type == IssueType.ANNUAL
        ):
            stripped_match = _matcher.match(
                series_name,
                stripped_wanted,
                alternate_names,
                fuzzy_high_threshold=self._config.fuzzy_high_threshold,
                fuzzy_low_threshold=self._config.fuzzy_low_threshold,
            )
            if stripped_match.similarity > match_result.similarity:
                match_result = stripped_match
        volume_hint = metadata.diagnostics.get("volume_subtitle_hint")
        if isinstance(volume_hint, dict):
            hint_base_series = str(volume_hint.get("base_series") or "").strip()
            hint_subtitle = str(volume_hint.get("subtitle") or "").strip()
            if hint_base_series and hint_subtitle:
                combined_title = f"{hint_base_series} {hint_subtitle}".strip()
                combined_match = _matcher.match(
                    combined_title,
                    wanted_series,
                    alternate_names,
                    fuzzy_high_threshold=self._config.fuzzy_high_threshold,
                    fuzzy_low_threshold=self._config.fuzzy_low_threshold,
                )
                if combined_match.similarity > match_result.similarity:
                    match_result = combined_match

        compatibility = issue_type_compatibility(
            parsed_type=metadata.issue_type,
            wanted_type=wanted_issue_type,
            policy=self._policy,
        )
        if not compatibility.compatible:
            return IssueMatchDecision(
                is_match=False,
                confidence=MatchConfidence.LOW,
                match_method="type_mismatch",
                rejection_reason=(
                    f"Issue type mismatch: {metadata.issue_type} vs {wanted_issue_type}"
                ),
                match_diagnostics={
                    "type_mode": compatibility.mode,
                    "series_similarity": round(match_result.similarity, 4),
                    "match_type": match_result.match_type,
                },
            )

        single_word_collection_prefix = False
        if not match_result.is_match and _is_safe_single_word_collection_prefix(
            policy=self._policy,
            metadata=metadata,
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_issue_type=wanted_issue_type,
            wanted_series_issue_count=wanted_series_issue_count,
        ):
            match_result = NameMatchResult(
                is_match=True,
                similarity=0.95,
                match_type="starts_with",
                matched_against=wanted_series,
            )
            single_word_collection_prefix = True
        if not match_result.is_match:
            return IssueMatchDecision(
                is_match=False,
                confidence=MatchConfidence.LOW,
                match_method="series_mismatch",
                rejection_reason=(
                    f"Series mismatch: '{series_name}' vs '{wanted_series}' "
                    f"(similarity={match_result.similarity:.2f})"
                ),
                match_diagnostics={"type_mode": compatibility.mode},
            )

        if compatibility.mode == "issue_to_collection_inference":
            inference_rejection = _validate_issue_to_collection_inference(
                metadata=metadata,
                wanted_issue=wanted_issue,
                wanted_issue_title=wanted_issue_title,
                wanted_series_issue_count=wanted_series_issue_count,
            )
            if inference_rejection is not None:
                return inference_rejection

        issue_check_skipped = False
        issue_confirmed = False
        match_method = "series_issue_match"
        issue_id_signal = metadata.signals.get("comicvine_issue_id")

        if wanted_issue_cv_id is not None and metadata.comicvine_issue_id is not None:
            if metadata.comicvine_issue_id == wanted_issue_cv_id:
                issue_confirmed = True
                match_method = "comicvine_issue_id"
            elif issue_id_signal in {
                MetadataSignal.COMICINFO,
                MetadataSignal.SIDECAR,
                MetadataSignal.MYLAR3,
            }:
                return IssueMatchDecision(
                    is_match=False,
                    confidence=MatchConfidence.LOW,
                    match_method="comicvine_issue_id_mismatch",
                    rejection_reason=(
                        "ComicVine issue ID mismatch: "
                        f"got {metadata.comicvine_issue_id} wanted {wanted_issue_cv_id}"
                    ),
                    match_diagnostics={
                        "type_mode": compatibility.mode,
                        "source_issue_cv_id": metadata.comicvine_issue_id,
                        "wanted_issue_cv_id": wanted_issue_cv_id,
                        "source_issue_cv_signal": issue_id_signal.value,
                    },
                )
        else:
            effective_issue = metadata.issue_number
            if effective_issue is None and metadata.volume and compatibility.prefer_volume_number:
                effective_issue = _normalize_volume_number(metadata.volume)

            wanted_is_collection = issue_type_family(wanted_issue_type) == TypeFamily.COLLECTION
            issue_omitted_ok = effective_issue is None and wanted_issue == 1.0
            subtitle_corroboration_required = (
                self._policy.require_volume_subtitle_corroboration
                or collection_title_subtitle(wanted_issue_title) is not None
                or _is_single_issue_subtitle_collection_target(
                    metadata=metadata,
                    wanted_series=wanted_series,
                    wanted_issue=wanted_issue,
                    wanted_series_issue_count=wanted_series_issue_count,
                )
            )

            if (
                wanted_is_collection
                and compatibility.prefer_volume_number
                and _has_volume_subtitle_hint(metadata)
                and issues_match(wanted_issue, effective_issue)
                and subtitle_corroboration_required
            ):
                issue_confirmed = True
                match_method = "issue_number"
                subtitle_check = _validate_volume_subtitle_hint(
                    metadata=metadata,
                    wanted_series=wanted_series,
                    wanted_issue_title=wanted_issue_title,
                )
                if subtitle_check is not None:
                    return subtitle_check
            elif wanted_is_collection:
                if _explicit_collection_ordinal_matches(
                    metadata=metadata,
                    wanted_issue_title=wanted_issue_title,
                ) and not _has_volume_subtitle_hint(metadata):
                    issue_confirmed = True
                    match_method = "collection_title_ordinal"
                elif effective_issue is None:
                    issue_confirmed = True
                    if _is_explicit_single_collection_match(
                        metadata=metadata,
                        wanted_issue=wanted_issue,
                        wanted_issue_type=wanted_issue_type,
                        wanted_series_issue_count=wanted_series_issue_count,
                        compatibility_mode=compatibility.mode,
                        title_match_type=match_result.match_type,
                    ):
                        match_method = "explicit_single_collection"
                    else:
                        issue_check_skipped = True
                        match_method = "collection_series_match"
                elif issues_match(wanted_issue, effective_issue):
                    issue_confirmed = True
                    match_method = "issue_number"
                    if compatibility.prefer_volume_number and subtitle_corroboration_required:
                        subtitle_check = _validate_volume_subtitle_hint(
                            metadata=metadata,
                            wanted_series=wanted_series,
                            wanted_issue_title=wanted_issue_title,
                        )
                        if subtitle_check is not None:
                            return subtitle_check
                elif (
                    wanted_issue == 1.0
                    and _has_volume_subtitle_hint(metadata)
                    and subtitle_corroboration_required
                ):
                    subtitle_check = _validate_volume_subtitle_hint(
                        metadata=metadata,
                        wanted_series=wanted_series,
                        wanted_issue_title=wanted_issue_title,
                    )
                    if subtitle_check is not None:
                        return subtitle_check
                    issue_confirmed = True
                    match_method = "single_issue_collection_volume_subtitle"
                else:
                    return IssueMatchDecision(
                        is_match=False,
                        confidence=MatchConfidence.LOW,
                        match_method="issue_mismatch",
                        rejection_reason=(
                            f"Issue mismatch: got {effective_issue} wanted {wanted_issue}"
                        ),
                        match_diagnostics={"type_mode": compatibility.mode},
                    )
            elif issues_match(wanted_issue, effective_issue) or (
                compatibility.allow_issue_number_fallback
                and issues_match(wanted_issue, effective_issue)
            ):
                issue_confirmed = True
                match_method = "issue_number"
                if compatibility.prefer_volume_number:
                    subtitle_check = _validate_volume_subtitle_hint(
                        metadata=metadata,
                        wanted_series=wanted_series,
                        wanted_issue_title=wanted_issue_title,
                    )
                    if subtitle_check is not None:
                        return subtitle_check
            elif issue_omitted_ok and _allows_implicit_issue_one(metadata):
                issue_confirmed = True
                issue_check_skipped = True
                match_method = "implicit_issue_one"
            elif wanted_issue == 1.0 and embedded_issue_number_title_match(
                source_series_name=metadata.series_name,
                original_title=metadata.original_title,
                target_series_title=wanted_series,
                issue_number=effective_issue,
            ):
                issue_confirmed = True
                match_method = "single_issue_embedded_number_title"
            else:
                return IssueMatchDecision(
                    is_match=False,
                    confidence=MatchConfidence.LOW,
                    match_method="issue_mismatch",
                    rejection_reason=f"Issue mismatch: got {effective_issue} wanted {wanted_issue}",
                    match_diagnostics={"type_mode": compatibility.mode},
                )

        year_evidence = match_release_year(
            metadata.year,
            wanted_year=wanted_year,
            volume_year=metadata.parsed_release.volume_year if metadata.parsed_release else None,
            context=year_context,
            issue_type=wanted_issue_type,
            tolerance=self._config.year_tolerance,
        )

        confidence = self._compute_confidence(
            match_type=match_result.match_type,
            similarity=match_result.similarity,
            year_match=year_evidence.matches,
        )
        if year_evidence.weak and confidence is MatchConfidence.HIGH:
            confidence = MatchConfidence.MEDIUM
        if compatibility.lowers_confidence:
            confidence = _lower_confidence(confidence)
        if issue_check_skipped and wanted_issue_type not in {IssueType.ISSUE, IssueType.ANNUAL}:
            confidence = _lower_confidence(confidence)

        return IssueMatchDecision(
            is_match=issue_confirmed,
            confidence=confidence,
            match_method=match_method,
            lowers_confidence=compatibility.lowers_confidence,
            match_diagnostics={
                "type_mode": compatibility.mode,
                "issue_check_skipped": issue_check_skipped,
                "series_similarity": round(match_result.similarity, 4),
                "match_type": match_result.match_type,
                "single_word_collection_prefix": single_word_collection_prefix,
                "year_match": year_evidence.matches,
                "year_match_basis": year_evidence.basis,
            },
        )

    def score_series_search_result(
        self,
        *,
        metadata: SourceMetadata,
        candidate: SeriesSearchResult,
    ) -> SeriesMatchDecision:
        """Score a series search result using shared type semantics plus name/year similarity."""
        raw_name = metadata.series_name or metadata.original_title
        raw_year = metadata.year
        normalized_query = NameMatcher.normalize(raw_name)
        normalized_candidate = NameMatcher.normalize(candidate.title)
        match = _matcher.match(
            normalized_query,
            normalized_candidate,
            fuzzy_high_threshold=self._config.fuzzy_high_threshold,
            fuzzy_low_threshold=self._config.fuzzy_low_threshold,
        )
        title_score = match.similarity * 0.70
        year_score = 0.0
        if raw_year is not None and candidate.year_start is not None:
            diff = abs(raw_year - candidate.year_start)
            if diff == 0:
                year_score = 0.25
            elif diff == 1:
                year_score = 0.15
            elif diff <= 3:
                year_score = 0.05
        publisher_score = 0.05 if candidate.publisher else 0.0
        shape_adjustment = self._series_shape_adjustment(metadata, candidate)
        score = min(max(title_score + year_score + publisher_score + shape_adjustment, 0.0), 1.0)
        return SeriesMatchDecision(
            score=score,
            compatible_type=True,
            match_method="series_title_year",
            diagnostics={
                "title_similarity": round(match.similarity, 4),
                "match_type": match.match_type,
                "shape_adjustment": round(shape_adjustment, 4),
            },
        )

    def _series_shape_adjustment(
        self,
        metadata: SourceMetadata,
        candidate: SeriesSearchResult,
    ) -> float:
        """Bias series scoring using shared non-standard type semantics and source shape hints."""
        issue_count = candidate.issue_count or 0
        status = (candidate.status or "").strip().lower()
        ended = status in {"ended", "complete", "completed", "finished"}
        source_family = issue_type_family(metadata.issue_type)
        adjustment = 0.0

        if metadata.issue_count_hint and issue_count:
            if abs(issue_count - metadata.issue_count_hint) <= 1:
                adjustment += 0.03
            elif issue_count > max(metadata.issue_count_hint * 2, metadata.issue_count_hint + 3):
                adjustment -= 0.03

        if metadata.issue_number is not None and issue_count:
            if float(metadata.issue_number) <= float(issue_count):
                adjustment += 0.08
            else:
                adjustment -= 0.05

        if source_family == TypeFamily.COLLECTION or metadata.issue_type in {
            IssueType.ONE_SHOT,
            IssueType.SPECIAL,
        }:
            if issue_count == 1:
                adjustment += 0.08
            elif 1 < issue_count <= 3:
                adjustment += 0.03
            elif issue_count >= 5:
                adjustment -= 0.08
            if ended:
                adjustment += 0.02
        elif source_family == TypeFamily.STANDARD:
            if issue_count == 1 and ended:
                adjustment -= 0.08
            elif 1 < issue_count <= 3 and ended:
                adjustment -= 0.03
            elif issue_count >= 5:
                adjustment += 0.02

        return adjustment

    def _compute_confidence(
        self,
        *,
        match_type: str,
        similarity: float,
        year_match: bool | None,
    ) -> MatchConfidence:
        if match_type in ("exact", "alternate"):
            if year_match is True:
                return MatchConfidence.HIGH
            if year_match is None:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW
        if match_type == "starts_with":
            if year_match is True:
                return MatchConfidence.HIGH
            if year_match is None:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW
        if match_type in ("token_set", "token_subset"):
            if year_match is True:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW
        if similarity >= self._config.fuzzy_high_threshold:
            if year_match is True:
                return MatchConfidence.MEDIUM
            return MatchConfidence.LOW
        return MatchConfidence.LOW


def _lower_confidence(confidence: MatchConfidence) -> MatchConfidence:
    if confidence == MatchConfidence.HIGH:
        return MatchConfidence.MEDIUM
    return MatchConfidence.LOW


def _is_safe_single_word_collection_prefix(
    *,
    policy: WorkflowPolicy,
    metadata: SourceMetadata,
    wanted_series: str,
    wanted_issue: float,
    wanted_year: int | None,
    wanted_issue_type: IssueType,
    wanted_series_issue_count: int | None,
) -> bool:
    """Allow a subtitle-bearing one-word title only for an exact-year singleton collection."""
    if (
        policy.name != "search"
        or issue_type_family(wanted_issue_type) != TypeFamily.COLLECTION
        or wanted_issue != 1.0
        or wanted_series_issue_count != 1
        or metadata.issue_number is not None
        or metadata.volume is not None
        or wanted_year is None
        or metadata.year != wanted_year
    ):
        return False

    normalized_target = NameMatcher.normalize(wanted_series)
    normalized_candidate = NameMatcher.normalize(metadata.series_name or "")
    if len(normalized_target.split()) != 1:
        return False
    prefix = f"{normalized_target} "
    if not normalized_candidate.startswith(prefix):
        return False
    return len(normalized_candidate.removeprefix(prefix).split()) >= 2


def _normalize_volume_number(volume: str) -> float | None:
    normalized = normalize_issue_number(volume)
    if normalized is not None:
        return normalized
    match = re.search(r"(\d+(?:\.\d+)?)", volume)
    if match:
        return normalize_issue_number(match.group(1))
    return None


def _validate_volume_subtitle_hint(
    *,
    metadata: SourceMetadata,
    wanted_series: str,
    wanted_issue_title: str | None,
) -> IssueMatchDecision | None:
    """Reject risky volume-number matches unless target text confirms the subtitle."""
    hint = metadata.diagnostics.get("volume_subtitle_hint")
    if not isinstance(hint, dict):
        return None
    subtitle = str(hint.get("subtitle") or "").strip()
    if not subtitle:
        return None

    subtitle_tokens = _subtitle_tokens(subtitle)
    series_title_tokens = _subtitle_tokens(wanted_series)
    if subtitle_tokens and subtitle_tokens.issubset(series_title_tokens):
        return None

    if not wanted_issue_title:
        return IssueMatchDecision(
            is_match=False,
            confidence=MatchConfidence.LOW,
            match_method="issue_title_mismatch",
            rejection_reason="Issue title missing for volume subtitle match.",
            match_diagnostics={"volume_subtitle_hint": hint},
        )

    issue_title_tokens = _subtitle_tokens(wanted_issue_title)
    if subtitle_tokens and subtitle_tokens.issubset(issue_title_tokens):
        return None

    return IssueMatchDecision(
        is_match=False,
        confidence=MatchConfidence.LOW,
        match_method="issue_title_mismatch",
        rejection_reason=(
            f"Issue title mismatch: subtitle '{subtitle}' not found in target titles."
        ),
        match_diagnostics={"volume_subtitle_hint": hint},
    )


def _validate_issue_to_collection_inference(
    *,
    metadata: SourceMetadata,
    wanted_issue: float,
    wanted_issue_title: str | None,
    wanted_series_issue_count: int | None,
) -> IssueMatchDecision | None:
    """Require title evidence before treating a numbered standard issue as a collection."""
    if _explicit_collection_ordinal_matches(
        metadata=metadata,
        wanted_issue_title=wanted_issue_title,
    ):
        return None
    subtitle = collection_title_subtitle(wanted_issue_title)
    if subtitle is None and wanted_series_issue_count == 1 and metadata.issue_number is not None:
        return IssueMatchDecision(
            is_match=False,
            confidence=MatchConfidence.LOW,
            match_method="numbered_issue_collection_mismatch",
            rejection_reason=(
                "Numbered standard issue cannot satisfy a single-record collection target."
            ),
            match_diagnostics={"type_mode": "issue_to_collection_inference"},
        )
    if not subtitle:
        return None

    subtitle_tokens = _subtitle_tokens(subtitle)
    candidate_tokens = _subtitle_tokens(metadata.original_title)
    if subtitle_tokens and subtitle_tokens.issubset(candidate_tokens):
        return None

    if wanted_issue == 1.0 and metadata.issue_number is None:
        return None

    return IssueMatchDecision(
        is_match=False,
        confidence=MatchConfidence.LOW,
        match_method="ambiguous_issue_collection_type",
        rejection_reason=(
            "Ambiguous issue-vs-volume result: standard issue lacks collection title evidence."
        ),
        match_diagnostics={"type_mode": "issue_to_collection_inference"},
    )


def _explicit_collection_ordinal_matches(
    *,
    metadata: SourceMetadata,
    wanted_issue_title: str | None,
) -> bool:
    """Match an explicit Book/Volume ordinal without trusting a bare issue number."""
    wanted_ordinal = normalize_issue_number(collection_title_number(wanted_issue_title))
    if wanted_ordinal is None:
        return False
    return any(
        issues_match(wanted_ordinal, normalize_issue_number(match.group("number")))
        for match in _COLLECTION_ORDINAL_RE.finditer(metadata.original_title)
    )


def _is_explicit_single_collection_match(
    *,
    metadata: SourceMetadata,
    wanted_issue: float,
    wanted_issue_type: IssueType,
    wanted_series_issue_count: int | None,
    compatibility_mode: str,
    title_match_type: str,
) -> bool:
    """Trust a numberless collection when its explicit identity is otherwise exact."""
    return (
        wanted_issue == 1.0
        and wanted_series_issue_count == 1
        and issue_type_family(wanted_issue_type) == TypeFamily.COLLECTION
        and metadata.issue_type == wanted_issue_type
        and metadata.signals.get("issue_type") == MetadataSignal.RELEASE_TITLE
        and compatibility_mode == "exact"
        and title_match_type == "exact"
    )


def _has_volume_subtitle_hint(metadata: SourceMetadata) -> bool:
    """Return True when parsed metadata carries a volume subtitle hint."""
    return isinstance(metadata.diagnostics.get("volume_subtitle_hint"), dict)


def _is_single_issue_subtitle_collection_target(
    *,
    metadata: SourceMetadata,
    wanted_series: str,
    wanted_issue: float,
    wanted_series_issue_count: int | None,
) -> bool:
    """Identify ComicVine series that model one titled collection volume as issue one."""
    if wanted_issue != 1.0 or wanted_series_issue_count != 1:
        return False
    hint = metadata.diagnostics.get("volume_subtitle_hint")
    if not isinstance(hint, dict):
        return False
    base_series = NameMatcher.normalize(str(hint.get("base_series") or ""))
    target_series = NameMatcher.normalize(wanted_series)
    return bool(base_series and target_series.startswith(f"{base_series} "))


def _subtitle_tokens(value: str) -> set[str]:
    normalized = NameMatcher.normalize(value)
    return {
        token
        for token in normalized.split()
        if token and token not in {"a", "an", "the", "vol", "volume"}
    }


def build_semantic_config(
    *,
    ignore_words: list[str] | None = None,
    fuzzy_high_threshold: float | None = None,
    fuzzy_low_threshold: float | None = None,
    year_tolerance: int | None = None,
    warn_issue_mb: int | None = None,
    warn_collection_mb: int | None = None,
) -> SemanticMatchConfig:
    """Create a semantic config from optional keyword overrides."""
    return SemanticMatchConfig(
        ignore_words=ignore_words or [],
        fuzzy_high_threshold=fuzzy_high_threshold
        if fuzzy_high_threshold is not None
        else NameMatcher.DEFAULT_FUZZY_HIGH,
        fuzzy_low_threshold=fuzzy_low_threshold
        if fuzzy_low_threshold is not None
        else NameMatcher.DEFAULT_FUZZY_LOW,
        year_tolerance=year_tolerance if year_tolerance is not None else 1,
        warn_issue_mb=warn_issue_mb if warn_issue_mb is not None else 750,
        warn_collection_mb=warn_collection_mb if warn_collection_mb is not None else 50,
    )


def semantic_config_from_eval_kwargs(eval_kwargs: SearchEvalKwargs) -> SemanticMatchConfig:
    """Build shared semantic configuration from search evaluation kwargs."""
    return build_semantic_config(
        ignore_words=(
            eval_kwargs.get("ignore_words")
            if isinstance(eval_kwargs.get("ignore_words"), list)
            else None
        ),
        fuzzy_high_threshold=_as_float(eval_kwargs.get("fuzzy_high_threshold")),
        fuzzy_low_threshold=_as_float(eval_kwargs.get("fuzzy_low_threshold")),
        year_tolerance=_as_int(eval_kwargs.get("year_tolerance")),
        warn_issue_mb=_as_int(eval_kwargs.get("warn_issue_mb")),
        warn_collection_mb=_as_int(eval_kwargs.get("warn_collection_mb")),
    )


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
