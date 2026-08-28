"""Search result evaluation and search-log detail helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.release_validator import ReleaseValidator, ValidationResult
from pullbox.services.search_scoring import (
    DEFAULT_MAX_SIZE_MB,
    DEFAULT_MIN_SIZE_MB,
    normalize_source_priority,
    score_release,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.providers.base import ReleaseResult

logger = structlog.get_logger(__name__)
DEFAULT_MIN_SCORE = 10
MAX_REJECTED_DIAGNOSTICS = 25
SLOW_INDEXER_THRESHOLD_MS = 1000


def _release_source_rank(release: ReleaseResult, source_priority: list[str] | None) -> int:
    """Return the configured protocol lane for one already-validated result."""
    normalized = normalize_source_priority(source_priority)
    if normalized is None:
        return 0
    return {value: index for index, value in enumerate(normalized)}[release.protocol.value]


def _score_validation_result(
    validation: ValidationResult,
    *,
    confidence_blend: float,
    quality_weight: float,
    indexer_priority: int | None = None,
    min_size_mb: int = DEFAULT_MIN_SIZE_MB,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    preferred_format: str | None = None,
    seeder_tiers: tuple[int, int, int] | None = None,
    score_weights: tuple[float, float, float, float] | None = None,
    grabs_weight: int = 0,
    pack_penalty: int = -20,
    max_file_count: int = 5,
    preferred_language: str = "en",
    digital_bonus: int = 10,
) -> float:
    """Score a validated result using the same logic as evaluate_results()."""

    confidence_bonus = {
        MatchConfidence.HIGH: 100,
        MatchConfidence.MEDIUM: 70,
        MatchConfidence.LOW: 40,
    }
    quality = score_release(
        validation.release,
        indexer_priority,
        min_size_mb=min_size_mb,
        max_size_mb=max_size_mb,
        preferred_format=preferred_format,
        seeder_tiers=seeder_tiers,
        score_weights=score_weights,
        grabs_weight=grabs_weight,
        pack_penalty=pack_penalty,
        max_file_count=max_file_count,
        preferred_language=preferred_language,
        digital_bonus=digital_bonus,
    )
    bonus = confidence_bonus.get(validation.confidence, 0)
    return (quality * quality_weight) + (bonus * confidence_blend)


def _select_best_validation(
    matched: list[ValidationResult],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    confidence_blend: float = 0.40,
    indexer_priority: int | None = None,
    min_size_mb: int = DEFAULT_MIN_SIZE_MB,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    preferred_format: str | None = None,
    seeder_tiers: tuple[int, int, int] | None = None,
    score_weights: tuple[float, float, float, float] | None = None,
    grabs_weight: int = 0,
    pack_penalty: int = -20,
    max_file_count: int = 5,
    preferred_language: str = "en",
    digital_bonus: int = 10,
    source_priority: list[str] | None = None,
) -> ValidationResult | None:
    """Select the best validated result above the minimum score."""

    if not matched:
        return None

    quality_weight = 1.0 - confidence_blend
    scored: list[tuple[int, float, ValidationResult]] = []
    for validation in matched:
        score = _score_validation_result(
            validation,
            confidence_blend=confidence_blend,
            quality_weight=quality_weight,
            indexer_priority=indexer_priority,
            min_size_mb=min_size_mb,
            max_size_mb=max_size_mb,
            preferred_format=preferred_format,
            seeder_tiers=seeder_tiers,
            score_weights=score_weights,
            grabs_weight=grabs_weight,
            pack_penalty=pack_penalty,
            max_file_count=max_file_count,
            preferred_language=preferred_language,
            digital_bonus=digital_bonus,
        )
        if score >= min_score:
            scored.append(
                (
                    _release_source_rank(validation.release, source_priority),
                    score,
                    validation,
                )
            )

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], -item[1]))
    return scored[0][2]


def release_provenance_key(release: ReleaseResult) -> str:
    """Build a stable key for attaching per-query provenance to a release."""

    return "|".join([release.indexer_name, release.download_url, release.title])


def _match_reason_summary(validation: ValidationResult) -> str:
    """Build a short support-facing explanation for the selected result."""

    confidence = validation.confidence.value if validation.confidence else "unknown"
    match_type = validation.match_type or "unknown"
    return f"{confidence} confidence {match_type} match"


def _result_diagnostic(
    validation: ValidationResult,
    *,
    status: str,
    query_provenance: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a compact diagnostic row for search history details and logs."""

    diagnostic: dict[str, object] = {
        "status": status,
        "title": validation.release.title,
        "indexer": validation.release.indexer_name,
        "confidence": validation.confidence.value if validation.confidence else None,
        "series_similarity": round(validation.series_similarity, 3),
        "match_type": validation.match_type,
        "parsed_series": validation.parsed.series_name,
        "parsed_issue": validation.parsed.issue_number,
        "parsed_year": validation.parsed.year,
        "issue_type": (
            str(validation.parsed.issue_type.value) if validation.parsed.issue_type else None
        ),
        "rejection_reason": validation.rejection_reason,
    }
    if query_provenance is not None:
        query = query_provenance.get(release_provenance_key(validation.release))
        if query:
            diagnostic["query"] = query
    return diagnostic


def build_search_details(
    matched: list[ValidationResult],
    rejected: list[ValidationResult],
    *,
    search_time_ms: int | None = None,
    search_passes: int = 1,
    query_provenance: dict[str, str] | None = None,
    query_diagnostics: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a details blob for SearchLog from validation results."""
    all_results = [*matched, *rejected]
    resolved_query_diagnostics = query_diagnostics or []

    type_dist: dict[str, int] = {}
    for validation in all_results:
        parsed_type = (
            str(validation.parsed.issue_type.value) if validation.parsed.issue_type else "unknown"
        )
        type_dist[parsed_type] = type_dist.get(parsed_type, 0) + 1

    conf_breakdown: dict[str, int] = {}
    for validation in matched:
        conf = validation.confidence.value if validation.confidence else "unknown"
        conf_breakdown[conf] = conf_breakdown.get(conf, 0) + 1

    best_match: dict[str, object] | None = None
    if matched:
        best = matched[0]
        best_match = {
            "title": best.release.title,
            "indexer": best.release.indexer_name,
            "confidence": best.confidence.value if best.confidence else None,
            "reason_summary": _match_reason_summary(best),
            "series_similarity": round(best.series_similarity, 3),
            "parsed_series": best.parsed.series_name,
            "parsed_issue": best.parsed.issue_number,
            "parsed_year": best.parsed.year,
            "issue_type": str(best.parsed.issue_type.value) if best.parsed.issue_type else None,
            "size_bytes": best.release.size_bytes,
            "is_torrent": best.release.is_torrent,
        }
        if query_provenance is not None:
            query = query_provenance.get(release_provenance_key(best.release))
            if query:
                best_match["query"] = query

    matched_summary = [
        _result_diagnostic(
            validation,
            status="matched",
            query_provenance=query_provenance,
        )
        for validation in matched
    ]

    sorted_rejected = sorted(
        rejected,
        key=lambda validation: validation.series_similarity,
        reverse=True,
    )
    rejected_diagnostics = [
        {
            **_result_diagnostic(
                validation,
                status="rejected",
                query_provenance=query_provenance,
            ),
            "reason": validation.rejection_reason,
        }
        for validation in sorted_rejected[:MAX_REJECTED_DIAGNOSTICS]
    ]
    top_rejected = rejected_diagnostics[:3]
    slow_indexers: list[dict[str, object]] = []
    for query_diag in resolved_query_diagnostics:
        query_label = str(query_diag.get("query") or "")
        indexer_diagnostics = query_diag.get("indexers")
        if not isinstance(indexer_diagnostics, list):
            continue
        for indexer_diag in indexer_diagnostics:
            if not isinstance(indexer_diag, dict):
                continue
            elapsed_ms = indexer_diag.get("elapsed_ms")
            if not isinstance(elapsed_ms, int | float):
                continue
            if elapsed_ms < SLOW_INDEXER_THRESHOLD_MS:
                continue
            slow_indexers.append(
                {
                    "query": query_label,
                    "indexer": indexer_diag.get("indexer", "Unknown"),
                    "elapsed_ms": int(elapsed_ms),
                    "result_count": indexer_diag.get("result_count", 0),
                    "filtered_count": indexer_diag.get("filtered_count", 0),
                    "status": indexer_diag.get("status", "completed"),
                }
            )

    return {
        "best_match": best_match,
        "matched": matched_summary,
        "top_rejected": top_rejected,
        "rejected": rejected_diagnostics,
        "rejected_count": len(rejected),
        "rejected_diagnostics_count": len(rejected_diagnostics),
        "rejected_diagnostics_truncated": len(rejected) > len(rejected_diagnostics),
        "type_distribution": type_dist,
        "confidence_breakdown": conf_breakdown,
        "query_diagnostics": resolved_query_diagnostics,
        "slow_indexers": slow_indexers,
        "search_passes": search_passes,
        "search_time_ms": search_time_ms,
    }


def log_type_detection(
    matched: list[ValidationResult],
    rejected: list[ValidationResult],
    wanted_type: IssueType,
    wanted_series: str,
    wanted_issue: float,
    *,
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Log structured type detection metrics for search results."""
    active_logger = log or logger
    all_results = [*matched, *rejected]
    type_dist: dict[str, int] = {}
    rejected_by_type = 0

    for validation in all_results:
        parsed_type = (
            str(validation.parsed.issue_type.value) if validation.parsed.issue_type else "unknown"
        )
        type_dist[parsed_type] = type_dist.get(parsed_type, 0) + 1

    for validation in rejected:
        if validation.rejection_reason and "Issue type mismatch" in validation.rejection_reason:
            rejected_by_type += 1

    active_logger.debug(
        "search_type_detection",
        wanted_series=wanted_series,
        wanted_issue=wanted_issue,
        wanted_type=wanted_type.value,
        results_parsed=len(all_results),
        type_distribution=type_dist,
        rejected_by_type=rejected_by_type,
        matched=len(matched),
        rejected_total=len(rejected),
    )


def evaluate_results(
    results: list[ReleaseResult],
    indexer_priority: int | None,
    min_score: float,
    *,
    wanted_series: str | None = None,
    wanted_issue: float | None = None,
    wanted_year: int | None = None,
    wanted_issue_type: IssueType = IssueType.ISSUE,
    wanted_issue_title: str | None = None,
    alternate_names: list[str] | None = None,
    ignore_words: list[str] | None = None,
    fuzzy_high_threshold: float | None = None,
    fuzzy_low_threshold: float | None = None,
    year_tolerance: int | None = None,
    warn_issue_mb: int | None = None,
    warn_collection_mb: int | None = None,
    min_size_mb: int | None = None,
    max_size_mb: int | None = None,
    preferred_format: str | None = None,
    seeder_tiers: tuple[int, int, int] | None = None,
    score_weights: tuple[float, float, float, float] | None = None,
    confidence_blend: float | None = None,
    grabs_weight: int = 0,
    pack_penalty: int = -20,
    max_file_count: int = 5,
    preferred_language: str = "en",
    digital_bonus: int = 10,
    source_priority: list[str] | None = None,
    log_type_detection_func: Callable[
        [list[ValidationResult], list[ValidationResult], IssueType, str, float],
        None,
    ] = log_type_detection,
    log: structlog.stdlib.BoundLogger | None = None,
) -> ReleaseResult | None:
    """Rank results and return the best match above the minimum score."""
    if not results:
        return None

    active_logger = log or logger
    blend = confidence_blend if confidence_blend is not None else 0.40
    quality_weight = 1.0 - blend

    if wanted_series is not None and wanted_issue is not None:
        active_logger.debug(
            "search_evaluate_start",
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_type=str(wanted_issue_type),
            total_results=len(results),
            validation="enabled",
            min_score=min_score,
            confidence_blend=round(blend, 2),
        )

        validator = ReleaseValidator(
            ignore_words=ignore_words,
            fuzzy_high_threshold=fuzzy_high_threshold,
            fuzzy_low_threshold=fuzzy_low_threshold,
            year_tolerance=year_tolerance,
            warn_issue_mb=warn_issue_mb,
            warn_collection_mb=warn_collection_mb,
        )
        validated, rejected = validator.validate_all_results(
            results,
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_issue_type=wanted_issue_type,
            alternate_names=alternate_names,
            wanted_issue_title=wanted_issue_title,
        )

        log_type_detection_func(
            validated,
            rejected,
            wanted_issue_type,
            wanted_series,
            wanted_issue,
        )

        if not validated:
            active_logger.debug(
                "search_evaluate_no_matches",
                wanted_series=wanted_series,
                wanted_issue=wanted_issue,
                total_results=len(results),
            )
            return None

        scored_validations: list[tuple[int, float, ValidationResult]] = []
        for validation in validated:
            final = _score_validation_result(
                validation,
                confidence_blend=blend,
                quality_weight=quality_weight,
                indexer_priority=indexer_priority,
                min_size_mb=min_size_mb or DEFAULT_MIN_SIZE_MB,
                max_size_mb=max_size_mb or DEFAULT_MAX_SIZE_MB,
                preferred_format=preferred_format,
                seeder_tiers=seeder_tiers,
                score_weights=score_weights,
                grabs_weight=grabs_weight,
                pack_penalty=pack_penalty,
                max_file_count=max_file_count,
                preferred_language=preferred_language,
                digital_bonus=digital_bonus,
            )
            scored_validations.append(
                (
                    _release_source_rank(validation.release, source_priority),
                    final,
                    validation,
                )
            )

        scored_validations.sort(key=lambda item: (item[0], -item[1]))

        for rank, (_, score, validation) in enumerate(scored_validations[:5], 1):
            active_logger.debug(
                "search_score_breakdown",
                rank=rank,
                title=validation.release.title,
                score=round(score, 1),
                indexer=validation.release.indexer_name,
                above_threshold=score >= min_score,
            )

        below = sum(1 for _, score, _ in scored_validations if score < min_score)
        scored_validations = [
            (source_rank, score, validation)
            for source_rank, score, validation in scored_validations
            if score >= min_score
        ]
        if below:
            active_logger.debug(
                "search_below_threshold",
                rejected_count=below,
                min_score=min_score,
            )
        if not scored_validations:
            active_logger.debug(
                "search_evaluate_all_below_threshold",
                total=len(results),
                min_score=min_score,
            )
            return None

        _, best_score, best_validation = scored_validations[0]
        best = best_validation.release
        above_threshold = len(scored_validations)
    else:
        active_logger.warning(
            "search_evaluate_no_validation",
            total_results=len(results),
            reason="wanted_series or wanted_issue not provided",
        )
        scored_releases = [
            (
                _release_source_rank(release, source_priority),
                score_release(
                    release,
                    indexer_priority,
                    min_size_mb=min_size_mb or DEFAULT_MIN_SIZE_MB,
                    max_size_mb=max_size_mb or DEFAULT_MAX_SIZE_MB,
                    preferred_format=preferred_format,
                    seeder_tiers=seeder_tiers,
                    score_weights=score_weights,
                    grabs_weight=grabs_weight,
                    pack_penalty=pack_penalty,
                    max_file_count=max_file_count,
                    preferred_language=preferred_language,
                    digital_bonus=digital_bonus,
                ),
                release,
            )
            for release in results
        ]
        scored_releases.sort(key=lambda item: (item[0], -item[1]))
        scored_releases = [
            (source_rank, score, release)
            for source_rank, score, release in scored_releases
            if score >= min_score
        ]
        if not scored_releases:
            active_logger.debug(
                "search_evaluate_all_below_threshold",
                total=len(results),
                min_score=min_score,
            )
            return None

        _, best_score, best = scored_releases[0]
        above_threshold = len(scored_releases)

    active_logger.debug(
        "search_evaluate_best",
        wanted_series=wanted_series,
        wanted_issue=wanted_issue,
        total_results=len(results),
        above_threshold=above_threshold,
        best_title=best.title,
        best_score=round(best_score, 1),
        best_indexer=best.indexer_name,
    )
    return best
