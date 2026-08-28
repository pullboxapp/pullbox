"""Import matching and ComicVine candidate evaluation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.source_metadata import SourceMetadata
from pullbox.core.type_semantics import (
    TypeFamily,
    canonical_series_type_for_issue_type,
    issue_type_family,
)
from pullbox.models.issue import IssueType
from pullbox.services.import_alternate_release_matching import (
    evaluate_alternate_release_candidates,
    evaluate_alternate_signal_conflict,
    has_type_qualified_alternate_candidate,
    series_result_supports_issue_like_type,
)
from pullbox.services.import_cv_candidate_ranking import (
    build_ranked_candidate_diagnostics,
    build_selected_candidate_summary,
    resolve_candidate_match_method,
)
from pullbox.services.import_cv_search import search_with_retry
from pullbox.services.import_known_cv_match import (
    ComicVineMatchEvaluation,
)
from pullbox.services.import_known_cv_match import (
    explicit_provider_method as _explicit_provider_method,
)
from pullbox.services.import_known_cv_match import (
    get_series_cached as _get_series_cached,
)
from pullbox.services.import_known_cv_match import (
    known_cv_id_evaluation_from_metadata as _known_cv_id_evaluation_from_metadata,
)
from pullbox.services.import_known_cv_match import (
    known_cv_id_evaluation_from_source as _known_cv_id_evaluation_from_source,
)
from pullbox.services.import_known_cv_match import (
    trusted_known_cv_id_without_fetch as _trusted_known_cv_id_without_fetch,
)
from pullbox.services.import_match_candidates import (
    build_ambiguous_series_conflict_diagnostics,
    candidate_has_exact_title_match,
    candidate_has_precise_title_match,
    candidate_issue_number_is_covered,
    select_year_tolerant_exact_title_candidate,
)
from pullbox.services.import_match_candidates import (
    candidate_cv_id as _candidate_cv_id,
)
from pullbox.services.import_match_candidates import (
    ranked_candidate_diagnostics as _ranked_candidate_diagnostics,
)
from pullbox.services.import_match_candidates import (
    score_cv_result as score_cv_result,
)
from pullbox.services.import_match_candidates import (
    select_ranked_candidate as _select_ranked_candidate,
)
from pullbox.services.import_match_candidates import (
    should_keep_subtitle_correlated_winner as _should_keep_subtitle_correlated_winner,
)
from pullbox.services.import_series_identity import DeduplicationResult, is_same_series
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine

if TYPE_CHECKING:
    from pullbox.models.series import SeriesType
    from pullbox.providers.base import SeriesSearchResult
    from pullbox.providers.metadata.comicvine import ComicVineProvider

logger = structlog.get_logger(__name__)

__all__ = [
    "ComicVineMatchEvaluation",
    "DeduplicationResult",
    "build_ambiguous_series_conflict_diagnostics",
    "is_same_series",
]

_MATCH_THRESHOLD = 0.80
_AMBIGUOUS_SERIES_MATCH_DELTA = 0.10

_YEAR_SENSITIVE_TITLE_MATCH_TYPES = frozenset({"fuzzy", "starts_with", "token_subset"})
_MAX_YEAR_SENSITIVE_MATCH_DELTA = 3
_ISSUE_YEAR_MATCH_TOLERANCE = 1
_ISSUE_LIKE_EXACT_EPSILON = 0.0001
_YEAR_IN_TEXT_RE = re.compile(r"\b((?:19|20)\d{2})\b")


@dataclass(frozen=True)
class IssueYearMismatch:
    """Confirmed issue-level year mismatch for an otherwise plausible series."""

    issue_year: int
    issue_year_delta: int


def series_type_from_import_diagnostics(
    diagnostics: dict[str, object] | None,
) -> SeriesType | None:
    """Map persisted import diagnostics onto a canonical SeriesType when possible."""
    if not diagnostics:
        return None
    raw_issue_type = diagnostics.get("source_issue_type")
    if not raw_issue_type:
        return None
    try:
        return canonical_series_type_for_issue_type(IssueType(str(raw_issue_type)))
    except (ValueError, KeyError):
        return None


def _year_delta_for_result(raw_year: int | None, result: SeriesSearchResult) -> int | None:
    return (
        abs(raw_year - result.year_start)
        if raw_year is not None and result.year_start is not None
        else None
    )


def _has_blocking_candidate_year_mismatch(
    *,
    raw_year: int | None,
    source_metadata: SourceMetadata,
    candidate: dict[str, Any],
    result: SeriesSearchResult,
) -> bool:
    selected_year_delta = _year_delta_for_result(raw_year, result)
    return (
        selected_year_delta is not None
        and selected_year_delta > _MAX_YEAR_SENSITIVE_MATCH_DELTA
        and str(candidate.get("match_type") or "") in _YEAR_SENSITIVE_TITLE_MATCH_TYPES
        and not candidate_issue_number_is_covered(
            source_metadata=source_metadata,
            candidate=candidate,
        )
    )


def _year_from_text(value: object) -> int | None:
    match = _YEAR_IN_TEXT_RE.search(str(value or ""))
    return int(match.group(1)) if match else None


async def _confirmed_issue_year_mismatch(
    *,
    provider: Any,
    source_metadata: SourceMetadata,
    selected_result: SeriesSearchResult,
    selected_candidate: dict[str, Any],
) -> IssueYearMismatch | None:
    """Return a blocking mismatch when the selected issue's date contradicts the file year."""
    if source_metadata.year is None or source_metadata.issue_number is None:
        return None
    selected_year = selected_result.year_start
    if selected_year is None:
        return None
    if abs(source_metadata.year - selected_year) <= _MAX_YEAR_SENSITIVE_MATCH_DELTA:
        return None
    if not candidate_issue_number_is_covered(
        source_metadata=source_metadata,
        candidate=selected_candidate,
    ):
        return None

    issue_lookup = _explicit_provider_method(provider, "get_issues_for_series_by_numbers")
    if issue_lookup is None:
        return None

    try:
        summaries = await issue_lookup(
            str(selected_result.provider_id),
            [float(source_metadata.issue_number)],
        )
    except Exception:
        logger.debug(
            "import_cv_issue_year_check_failed",
            cv_id=selected_result.provider_id,
            issue_number=source_metadata.issue_number,
            exc_info=True,
        )
        return None

    for summary in summaries or []:
        summary_issue_number = getattr(summary, "issue_number", None)
        if summary_issue_number is None:
            continue
        try:
            issue_matches = (
                abs(float(summary_issue_number) - float(source_metadata.issue_number)) <= 0.0001
            )
        except (TypeError, ValueError):
            issue_matches = False
        if not issue_matches:
            continue

        issue_year = _year_from_text(getattr(summary, "release_date", None))
        if issue_year is None:
            continue
        issue_year_delta = abs(source_metadata.year - issue_year)
        if issue_year_delta > _ISSUE_YEAR_MATCH_TOLERANCE:
            return IssueYearMismatch(
                issue_year=issue_year,
                issue_year_delta=issue_year_delta,
            )
    return None


async def match_to_comicvine(
    *,
    provider: ComicVineProvider,
    raw_name: str,
    raw_year: int | None,
    source_metadata: SourceMetadata | None = None,
    mylar3_cv_id: int | None = None,
    comicinfo_cv_id: int | None = None,
    match_threshold: float = _MATCH_THRESHOLD,
) -> dict[str, Any] | None:
    """Match a discovered series to ComicVine."""
    evaluation = await evaluate_comicvine_match(
        provider=provider,
        raw_name=raw_name,
        raw_year=raw_year,
        source_metadata=source_metadata,
        mylar3_cv_id=mylar3_cv_id,
        comicinfo_cv_id=comicinfo_cv_id,
        match_threshold=match_threshold,
    )
    return evaluation.match


async def evaluate_comicvine_match(
    *,
    provider: ComicVineProvider,
    raw_name: str,
    raw_year: int | None,
    source_metadata: SourceMetadata | None = None,
    mylar3_cv_id: int | None = None,
    comicinfo_cv_id: int | None = None,
    match_threshold: float = _MATCH_THRESHOLD,
) -> ComicVineMatchEvaluation:
    """Return the best ComicVine match plus structured scoring diagnostics."""
    source_metadata = source_metadata or SourceMetadata(
        original_title=raw_name,
        series_name=raw_name,
        year=raw_year,
    )
    normalized_query = NameMatcher.normalize(source_metadata.series_name or raw_name)
    semantic_engine = SemanticMatchEngine(policy=ImportPolicy())

    identity_conflicts = source_metadata.diagnostics.get("identity_conflicts")
    if isinstance(identity_conflicts, list) and identity_conflicts:
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_conflict",
                "reason": "trusted_source_identity_conflict",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "identity_conflicts": [
                    dict(conflict) for conflict in identity_conflicts if isinstance(conflict, dict)
                ],
                "top_candidates": [],
            },
        )

    known_cv_id = mylar3_cv_id or comicinfo_cv_id or source_metadata.comicvine_series_id
    if known_cv_id:
        match_method = "mylar3_cv_id" if mylar3_cv_id else "comicinfo_cv_id"
        if source_metadata.comicvine_series_id and not mylar3_cv_id and not comicinfo_cv_id:
            match_method = "comicinfo_cv_id"
        cached_meta = await _get_series_cached(provider, str(known_cv_id))
        if cached_meta is not None:
            return _known_cv_id_evaluation_from_metadata(
                cached_meta,
                match_method=match_method,
                raw_name=raw_name,
                raw_year=raw_year,
                normalized_query=normalized_query,
                match_threshold=match_threshold,
                reason="known_cv_id_cached",
            )
        if _trusted_known_cv_id_without_fetch(
            mylar3_cv_id=mylar3_cv_id,
            comicinfo_cv_id=comicinfo_cv_id,
            source_metadata=source_metadata,
        ):
            return _known_cv_id_evaluation_from_source(
                int(known_cv_id),
                match_method=match_method,
                raw_name=raw_name,
                raw_year=raw_year,
                normalized_query=normalized_query,
                source_metadata=source_metadata,
                match_threshold=match_threshold,
            )
        try:
            meta = await provider.get_series(str(known_cv_id))
            return _known_cv_id_evaluation_from_metadata(
                meta,
                match_method=match_method,
                raw_name=raw_name,
                raw_year=raw_year,
                normalized_query=normalized_query,
                match_threshold=match_threshold,
                reason="known_cv_id",
            )
        except Exception:
            logger.warning(
                "import_cv_id_lookup_failed",
                cv_id=known_cv_id,
                method=match_method,
            )

    search_results = await search_with_retry(provider, raw_name, raw_year)
    if not search_results:
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_no_match",
                "reason": "no_results",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "top_candidates": [],
            },
        )

    candidate_diagnostics = build_ranked_candidate_diagnostics(
        raw_name=raw_name,
        raw_year=raw_year,
        source_metadata=source_metadata,
        search_results=search_results,
        semantic_engine=semantic_engine,
        match_threshold=match_threshold,
    )
    top_candidates: list[dict[str, Any]] = candidate_diagnostics[:3]
    selected_candidate = _select_ranked_candidate(
        candidate_diagnostics=candidate_diagnostics,
        search_results=search_results,
        match_threshold=match_threshold,
    )

    if selected_candidate is None:
        alternate_evaluation = await evaluate_alternate_release_candidates(
            provider=provider,
            source_metadata=source_metadata,
            raw_name=raw_name,
            raw_year=raw_year,
            semantic_engine=semantic_engine,
            match_threshold=match_threshold,
            existing_top_candidates=top_candidates,
        )
        if alternate_evaluation is not None:
            return alternate_evaluation
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_no_match",
                "reason": "below_threshold",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "top_candidates": top_candidates,
            },
        )

    best_candidate, best_result = selected_candidate
    best_score = float(best_candidate.get("score", 0.0))
    alternate_is_type_boundary = issue_type_family(
        source_metadata.issue_type
    ) == TypeFamily.ISSUE_LIKE or has_type_qualified_alternate_candidate(source_metadata)
    if source_metadata.issue_type == IssueType.ANNUAL or has_type_qualified_alternate_candidate(
        source_metadata
    ):
        alternate_evaluation = await evaluate_alternate_release_candidates(
            provider=provider,
            source_metadata=source_metadata,
            raw_name=raw_name,
            raw_year=raw_year,
            semantic_engine=semantic_engine,
            match_threshold=match_threshold,
            existing_top_candidates=top_candidates,
        )
        alternate_selected = (
            alternate_evaluation.diagnostics.get("selected_candidate")
            if alternate_evaluation is not None
            else None
        )
        if (
            alternate_evaluation is not None
            and alternate_evaluation.match is not None
            and isinstance(alternate_selected, dict)
            and candidate_has_exact_title_match(alternate_selected)
            and (
                not candidate_has_exact_title_match(best_candidate or {})
                or float(alternate_evaluation.match.get("cv_match_score") or 0.0) > best_score
                or (
                    alternate_is_type_boundary
                    and float(alternate_evaluation.match.get("cv_match_score") or 0.0)
                    >= best_score - _ISSUE_LIKE_EXACT_EPSILON
                )
            )
        ):
            return alternate_evaluation
    if best_candidate is not None:
        exact_title_candidate = select_year_tolerant_exact_title_candidate(
            source_metadata=source_metadata,
            current_candidate=best_candidate,
            candidate_diagnostics=candidate_diagnostics,
            match_threshold=match_threshold,
        )
        if exact_title_candidate is not None:
            exact_title_candidate_id = int(exact_title_candidate.get("cv_id") or 0)
            exact_title_result = next(
                (
                    result
                    for result in search_results
                    if int(result.provider_id) == exact_title_candidate_id
                ),
                None,
            )
            if exact_title_result is not None:
                best_candidate = exact_title_candidate
                best_result = exact_title_result
                best_score = float(exact_title_candidate.get("score", 0.0))

    if best_candidate is not None and str(best_candidate.get("match_type") or "") == "fuzzy":
        competing_candidate = next(
            (
                candidate
                for candidate in candidate_diagnostics
                if int(candidate.get("cv_id") or 0) != int(best_result.provider_id)
                and float(candidate.get("score", 0.0)) >= match_threshold
                and candidate_has_precise_title_match(candidate)
                and best_score - float(candidate.get("score", 0.0)) <= _AMBIGUOUS_SERIES_MATCH_DELTA
            ),
            None,
        )
        if competing_candidate is not None and not _should_keep_subtitle_correlated_winner(
            source_metadata=source_metadata,
            selected_candidate=best_candidate,
            competing_candidate=competing_candidate,
        ):
            return ComicVineMatchEvaluation(
                match=None,
                diagnostics=build_ambiguous_series_conflict_diagnostics(
                    raw_name=raw_name,
                    raw_year=raw_year,
                    match_threshold=match_threshold,
                    selected_candidate=build_selected_candidate_summary(
                        best_result,
                        score=best_score,
                        match_method="fuzzy_title",
                        match_type=str(best_candidate.get("match_type") or ""),
                    ),
                    competing_candidate=competing_candidate,
                    top_candidates=top_candidates,
                ),
            )

    best_match_type = str((best_candidate or {}).get("match_type") or "")
    selected_year_delta = _year_delta_for_result(raw_year, best_result)
    match_method = resolve_candidate_match_method(
        best_candidate or {},
        selected_year_delta=selected_year_delta,
    )

    if issue_type_family(
        source_metadata.issue_type
    ) == TypeFamily.ISSUE_LIKE and not series_result_supports_issue_like_type(
        source_metadata.issue_type,
        best_result,
        best_candidate,
    ):
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_no_match",
                "reason": "issue_like_type_mismatch",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "selected_candidate": build_selected_candidate_summary(
                    best_result,
                    score=best_score,
                    match_method=match_method,
                    match_type=best_match_type,
                    year_delta=selected_year_delta,
                ),
                "top_candidates": top_candidates,
            },
        )

    signal_conflict = await evaluate_alternate_signal_conflict(
        provider=provider,
        source_metadata=source_metadata,
        raw_name=raw_name,
        raw_year=raw_year,
        semantic_engine=semantic_engine,
        match_threshold=match_threshold,
        selected_result=best_result,
        selected_candidate=best_candidate or {},
        selected_score=best_score,
        selected_match_method=match_method,
        selected_match_type=best_match_type,
        existing_top_candidates=top_candidates,
    )
    if signal_conflict is not None:
        return signal_conflict

    if _has_blocking_candidate_year_mismatch(
        raw_year=raw_year,
        source_metadata=source_metadata,
        candidate=best_candidate or {},
        result=best_result,
    ):
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_no_match",
                "reason": "blocking_year_mismatch",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "selected_candidate": build_selected_candidate_summary(
                    best_result,
                    score=best_score,
                    match_method=match_method,
                    match_type=best_match_type,
                    year_delta=selected_year_delta,
                ),
                "top_candidates": top_candidates,
            },
        )

    if best_candidate is not None:
        issue_year_mismatch = await _confirmed_issue_year_mismatch(
            provider=provider,
            source_metadata=source_metadata,
            selected_result=best_result,
            selected_candidate=best_candidate,
        )
        if issue_year_mismatch is not None:
            rejected_candidate_ids = {int(best_result.provider_id)}
            replacement = None
            for replacement_candidate in _ranked_candidate_diagnostics(candidate_diagnostics):
                replacement_id = _candidate_cv_id(replacement_candidate)
                if replacement_id is None or replacement_id in rejected_candidate_ids:
                    continue
                replacement = _select_ranked_candidate(
                    candidate_diagnostics=[replacement_candidate],
                    search_results=search_results,
                    match_threshold=match_threshold,
                    excluded_candidate_ids=rejected_candidate_ids,
                )
                if replacement is None:
                    continue
                replacement_candidate, replacement_result = replacement
                replacement_mismatch = await _confirmed_issue_year_mismatch(
                    provider=provider,
                    source_metadata=source_metadata,
                    selected_result=replacement_result,
                    selected_candidate=replacement_candidate,
                )
                if replacement_mismatch is not None:
                    rejected_candidate_ids.add(replacement_id)
                    replacement = None
                    continue
                if _has_blocking_candidate_year_mismatch(
                    raw_year=raw_year,
                    source_metadata=source_metadata,
                    candidate=replacement_candidate,
                    result=replacement_result,
                ):
                    rejected_candidate_ids.add(replacement_id)
                    replacement = None
                    continue
                break

            if replacement is None:
                return ComicVineMatchEvaluation(
                    match=None,
                    diagnostics={
                        "kind": "series_no_match",
                        "reason": "blocking_issue_year_mismatch",
                        "raw_name": raw_name,
                        "raw_year": raw_year,
                        "normalized_query": normalized_query,
                        "threshold": round(match_threshold, 4),
                        "selected_candidate": build_selected_candidate_summary(
                            best_result,
                            score=best_score,
                            match_method=match_method,
                            match_type=best_match_type,
                            year_delta=selected_year_delta,
                            extra={
                                "issue_year": issue_year_mismatch.issue_year,
                                "issue_year_delta": issue_year_mismatch.issue_year_delta,
                            },
                        ),
                        "top_candidates": top_candidates,
                    },
                )

            best_candidate, best_result = replacement
            best_score = float(best_candidate.get("score", 0.0))
            best_match_type = str(best_candidate.get("match_type") or "")
            selected_year_delta = _year_delta_for_result(raw_year, best_result)
            match_method = resolve_candidate_match_method(
                best_candidate,
                selected_year_delta=selected_year_delta,
            )

    return ComicVineMatchEvaluation(
        match={
            "cv_id": int(best_result.provider_id),
            "cv_title": best_result.title,
            "cv_year": best_result.year_start,
            "cv_publisher": best_result.publisher,
            "cv_issue_count": best_result.issue_count,
            "cv_url": None,
            "cv_match_score": round(best_score, 4),
            "cv_match_method": match_method,
        },
        diagnostics={
            "kind": "series_match",
            "reason": "matched",
            "raw_name": raw_name,
            "raw_year": raw_year,
            "normalized_query": normalized_query,
            "threshold": round(match_threshold, 4),
            "selected_candidate": build_selected_candidate_summary(
                best_result,
                score=best_score,
                match_method=match_method,
                match_type=best_match_type,
                year_delta=selected_year_delta,
            ),
            "top_candidates": top_candidates,
        },
    )
