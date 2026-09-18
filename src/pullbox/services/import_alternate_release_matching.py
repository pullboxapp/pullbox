"""Alternate release-title matching helpers for import series matching."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.core.type_semantics import (
    TypeFamily,
    issue_type_family,
    title_supports_issue_like_type,
)
from pullbox.models.issue import IssueType
from pullbox.services.import_cv_candidate_ranking import build_selected_candidate_summary
from pullbox.services.import_cv_search import search_with_retry
from pullbox.services.import_known_cv_match import ComicVineMatchEvaluation
from pullbox.services.import_match_candidates import (
    build_ambiguous_series_conflict_diagnostics,
    build_candidate_diagnostics,
    build_signal_conflict_series_diagnostics,
    candidate_has_exact_title_match,
    candidate_has_precise_title_match,
)

if TYPE_CHECKING:
    from pullbox.providers.base import SeriesSearchResult
    from pullbox.providers.metadata.comicvine import ComicVineProvider
    from pullbox.services.semantic_matching import SemanticMatchEngine

_matcher = NameMatcher()
_AMBIGUOUS_SERIES_MATCH_DELTA = 0.10


def has_type_qualified_alternate_candidate(source_metadata: SourceMetadata) -> bool:
    """Return True when release-title parsing preserved a stronger type-qualified identity."""
    alternate_release_candidates = source_metadata.diagnostics.get("alternate_release_candidates")
    if not isinstance(alternate_release_candidates, list):
        return False
    return any(
        isinstance(alternate, dict) and alternate.get("issue_type_qualified") is True
        for alternate in alternate_release_candidates
    )


def series_result_supports_issue_like_type(
    issue_type: IssueType,
    result: SeriesSearchResult,
    candidate_diagnostics: dict[str, Any] | None = None,
) -> bool:
    """Return true when a series candidate safely represents an issue-like source."""
    if title_supports_issue_like_type(issue_type, result.title):
        return True
    if (
        issue_type in {IssueType.ONE_SHOT, IssueType.SPECIAL}
        and _candidate_issue_count(result) == 1
        and any(
            title_supports_issue_like_type(compatible_type, result.title)
            for compatible_type in (IssueType.ONE_SHOT, IssueType.SPECIAL)
            if compatible_type != issue_type
        )
    ):
        return True
    if issue_type != IssueType.ONE_SHOT:
        return False
    if _candidate_issue_count(result) != 1:
        return False
    return candidate_diagnostics is None or candidate_has_exact_title_match(candidate_diagnostics)


async def evaluate_alternate_signal_conflict(
    *,
    provider: ComicVineProvider,
    source_metadata: SourceMetadata,
    raw_name: str,
    raw_year: int | None,
    semantic_engine: SemanticMatchEngine,
    match_threshold: float,
    selected_result: SeriesSearchResult,
    selected_candidate: dict[str, Any],
    selected_score: float,
    selected_match_method: str,
    selected_match_type: str,
    existing_top_candidates: list[dict[str, Any]],
) -> ComicVineMatchEvaluation | None:
    """Return conflict diagnostics when embedded metadata and release title disagree."""
    alternate_release_candidates = source_metadata.diagnostics.get("alternate_release_candidates")
    if not (
        isinstance(alternate_release_candidates, list)
        and alternate_release_candidates
        and source_metadata.signals.get("series_name")
        in {MetadataSignal.COMICINFO, MetadataSignal.SIDECAR}
    ):
        return None

    selected_series_name = source_metadata.series_name or raw_name
    selected_signal = source_metadata.signals.get(
        "series_name",
        MetadataSignal.RELEASE_TITLE,
    ).value
    for alternate in alternate_release_candidates:
        if not isinstance(alternate, dict):
            continue
        alternate_series = str(alternate.get("series_name") or "").strip()
        if not alternate_series:
            continue
        if candidate_has_precise_title_match(
            {
                "match_type": _matcher.match(alternate_series, selected_series_name).match_type,
            }
        ):
            continue

        alternate_year = (
            int(alternate["year"]) if alternate.get("year") is not None else source_metadata.year
        )
        alternate_metadata = source_metadata.model_copy(
            update={
                "original_title": str(alternate.get("file_name") or raw_name),
                "series_name": alternate_series,
                "year": alternate_year,
                "signals": {
                    **dict(source_metadata.signals),
                    "series_name": MetadataSignal.RELEASE_TITLE,
                },
            }
        )
        alternate_results = await search_with_retry(provider, alternate_series, alternate_year)
        if not alternate_results:
            continue

        normalized_alternate_series = NameMatcher.normalize(alternate_series)
        exact_title_results = [
            candidate
            for candidate in alternate_results
            if NameMatcher.normalize(candidate.title) == normalized_alternate_series
        ]
        candidate_pool = exact_title_results or alternate_results
        alternate_best_result: SeriesSearchResult | None = None
        alternate_best_score = 0.0
        alternate_best_candidate: dict[str, Any] | None = None
        for candidate in candidate_pool:
            decision = semantic_engine.score_series_search_result(
                metadata=alternate_metadata,
                candidate=candidate,
            )
            candidate_diag = build_candidate_diagnostics(
                raw_name=alternate_series,
                raw_year=alternate_year,
                result=candidate,
                threshold=match_threshold,
                score_override=decision.score,
                extra_diagnostics=decision.diagnostics,
            )
            if decision.score > alternate_best_score or alternate_best_result is None:
                alternate_best_result = candidate
                alternate_best_score = decision.score
                alternate_best_candidate = candidate_diag

        if (
            alternate_best_result is None
            or alternate_best_candidate is None
            or alternate_best_score < match_threshold
            or int(alternate_best_result.provider_id) == int(selected_result.provider_id)
            or selected_score - alternate_best_score > _AMBIGUOUS_SERIES_MATCH_DELTA
        ):
            continue
        if candidate_has_exact_title_match(selected_candidate) and not (
            candidate_has_exact_title_match(alternate_best_candidate)
        ):
            continue

        combined_top_candidates = [
            *existing_top_candidates,
            alternate_best_candidate,
        ]
        deduped_top_candidates = _dedupe_and_sort_top_candidates(combined_top_candidates)

        return ComicVineMatchEvaluation(
            match=None,
            diagnostics=build_signal_conflict_series_diagnostics(
                raw_name=raw_name,
                raw_year=raw_year,
                match_threshold=match_threshold,
                selected_candidate=build_selected_candidate_summary(
                    selected_result,
                    score=selected_score,
                    match_method=selected_match_method,
                    match_type=selected_match_type,
                ),
                competing_candidate={
                    **alternate_best_candidate,
                    "match_method": "release_title_conflict",
                },
                top_candidates=deduped_top_candidates[:3],
                selected_signal=selected_signal,
                competing_signal=str(alternate.get("signal") or MetadataSignal.RELEASE_TITLE.value),
                signal_file_name=str(alternate.get("file_name") or ""),
            ),
        )

    return None


async def evaluate_alternate_release_candidates(
    *,
    provider: ComicVineProvider,
    source_metadata: SourceMetadata,
    raw_name: str,
    raw_year: int | None,
    semantic_engine: SemanticMatchEngine,
    match_threshold: float,
    existing_top_candidates: list[dict[str, Any]],
) -> ComicVineMatchEvaluation | None:
    """Try safe filename-derived alternate series names after the primary query misses."""
    alternate_release_candidates = source_metadata.diagnostics.get("alternate_release_candidates")
    if not isinstance(alternate_release_candidates, list):
        return None

    best_evaluation: ComicVineMatchEvaluation | None = None
    best_evaluation_key: tuple[int, float, int] | None = None
    source_issue_like_type = (
        source_metadata.issue_type
        if issue_type_family(source_metadata.issue_type) == TypeFamily.ISSUE_LIKE
        else None
    )

    for alternate in alternate_release_candidates:
        if not isinstance(alternate, dict):
            continue
        alternate_series = str(alternate.get("series_name") or "").strip()
        if not alternate_series:
            continue
        alternate_issue_like_type = _issue_like_type_from_alternate(alternate)
        if source_issue_like_type is not None:
            if alternate_issue_like_type is None and title_supports_issue_like_type(
                source_issue_like_type,
                alternate_series,
            ):
                alternate_issue_like_type = source_issue_like_type
            elif alternate_issue_like_type is None:
                continue
            if alternate_issue_like_type != source_issue_like_type:
                continue
        alternate_year = int(alternate["year"]) if alternate.get("year") is not None else raw_year
        alternate_metadata = source_metadata.model_copy(
            update={
                "original_title": str(alternate.get("file_name") or source_metadata.original_title),
                "series_name": alternate_series,
                "year": alternate_year,
                "signals": {
                    **dict(source_metadata.signals),
                    "series_name": MetadataSignal.RELEASE_TITLE,
                },
            }
        )
        alternate_results = await search_with_retry(provider, alternate_series, alternate_year)
        if not alternate_results:
            continue

        best_result: SeriesSearchResult | None = None
        best_candidate: dict[str, Any] | None = None
        best_score = 0.0
        candidate_diagnostics: list[dict[str, Any]] = []
        for candidate in alternate_results:
            decision = semantic_engine.score_series_search_result(
                metadata=alternate_metadata,
                candidate=candidate,
            )
            candidate_diag = build_candidate_diagnostics(
                raw_name=alternate_series,
                raw_year=alternate_year,
                result=candidate,
                threshold=match_threshold,
                score_override=decision.score,
                extra_diagnostics=decision.diagnostics,
            )
            if alternate_issue_like_type is not None and not (
                series_result_supports_issue_like_type(
                    alternate_issue_like_type,
                    candidate,
                    candidate_diag,
                )
            ):
                continue
            candidate_diagnostics.append(candidate_diag)
            if decision.score > best_score or best_result is None:
                best_result = candidate
                best_candidate = candidate_diag
                best_score = decision.score

        if best_result is None or best_candidate is None or best_score < match_threshold:
            continue

        top_candidates = [*existing_top_candidates, *candidate_diagnostics]
        top_candidates.sort(
            key=lambda candidate: (
                float(candidate.get("score", 0.0)),
                int(candidate.get("issue_count") or 0),
            ),
            reverse=True,
        )
        competing_exact_candidate = next(
            (
                candidate
                for candidate in candidate_diagnostics
                if int(candidate.get("cv_id") or 0) != int(best_result.provider_id)
                and candidate_has_exact_title_match(best_candidate)
                and candidate_has_exact_title_match(candidate)
                and candidate.get("normalized_title") == best_candidate.get("normalized_title")
                and candidate.get("year") == best_candidate.get("year")
                and float(candidate.get("score", 0.0)) >= match_threshold
                and abs(float(candidate.get("score", 0.0)) - best_score)
                <= _AMBIGUOUS_SERIES_MATCH_DELTA
            ),
            None,
        )
        if competing_exact_candidate is not None:
            return ComicVineMatchEvaluation(
                match=None,
                diagnostics=build_ambiguous_series_conflict_diagnostics(
                    raw_name=raw_name,
                    raw_year=raw_year,
                    match_threshold=match_threshold,
                    selected_candidate={
                        **best_candidate,
                        "match_method": "alternate_release_candidate",
                        "alternate_series_name": alternate_series,
                    },
                    competing_candidate={
                        **competing_exact_candidate,
                        "match_method": "alternate_release_candidate",
                        "alternate_series_name": alternate_series,
                    },
                    top_candidates=_dedupe_and_sort_top_candidates(top_candidates)[:3],
                ),
            )
        evaluation = ComicVineMatchEvaluation(
            match={
                "cv_id": int(best_result.provider_id),
                "cv_title": best_result.title,
                "cv_year": best_result.year_start,
                "cv_publisher": best_result.publisher,
                "cv_issue_count": best_result.issue_count,
                "cv_url": None,
                "cv_match_score": round(best_score, 4),
                "cv_match_method": "alternate_release_candidate",
            },
            diagnostics={
                "kind": "series_match",
                "reason": "alternate_release_candidate",
                "raw_name": raw_name,
                "raw_year": raw_year,
                "normalized_query": NameMatcher.normalize(raw_name),
                "threshold": round(match_threshold, 4),
                "selected_candidate": {
                    **best_candidate,
                    "match_method": "alternate_release_candidate",
                    "alternate_series_name": alternate_series,
                },
                "top_candidates": top_candidates[:3],
            },
        )
        evaluation_key = (
            1 if candidate_has_exact_title_match(best_candidate) else 0,
            round(best_score, 4),
            int(best_result.issue_count or 0),
        )
        if best_evaluation_key is None or evaluation_key > best_evaluation_key:
            best_evaluation = evaluation
            best_evaluation_key = evaluation_key

    return best_evaluation


def _candidate_issue_count(candidate: SeriesSearchResult) -> int | None:
    try:
        return int(candidate.issue_count) if candidate.issue_count is not None else None
    except (TypeError, ValueError):
        return None


def _issue_like_type_from_alternate(alternate: dict[str, Any]) -> IssueType | None:
    """Return the issue-like type carried by a type-qualified alternate candidate."""
    if alternate.get("issue_type_qualified") is not True:
        return None
    raw_issue_type = alternate.get("issue_type")
    if raw_issue_type is None:
        return None
    try:
        issue_type = IssueType(str(raw_issue_type))
    except ValueError:
        return None
    if issue_type_family(issue_type) != TypeFamily.ISSUE_LIKE:
        return None
    return issue_type


def _dedupe_and_sort_top_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped_top_candidates: list[dict[str, Any]] = []
    seen_candidate_ids: set[int] = set()
    for candidate_diag in candidates:
        candidate_id = int(candidate_diag.get("cv_id") or 0)
        if candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate_id)
        deduped_top_candidates.append(candidate_diag)
    deduped_top_candidates.sort(
        key=lambda candidate: (
            float(candidate.get("score", 0.0)),
            int(candidate.get("issue_count") or 0),
        ),
        reverse=True,
    )
    return deduped_top_candidates
