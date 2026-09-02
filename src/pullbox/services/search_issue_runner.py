"""Single-target issue search orchestration."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pullbox.services.search_evaluation import DEFAULT_MIN_SCORE
from pullbox.services.search_scoring import DEFAULT_MAX_SIZE_MB, DEFAULT_MIN_SIZE_MB
from pullbox.services.search_targets import IssueSearchOutcome

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    import structlog
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.models.issue import IssueType
    from pullbox.providers.base import ReleaseResult, SearchQuery
    from pullbox.services.release_validator import ReleaseValidator, ValidationResult
    from pullbox.services.search_targets import IssueSearchTarget
    from pullbox.services.search_types import IssueSearchMode, SearchEvalKwargs, ValidatorKwargs

    RunQueryBatchFunc = Callable[
        [list[SearchQuery]],
        Awaitable[list[ReleaseResult]],
    ]
    RunQueryBatchWithProvenanceFunc = Callable[
        [list[SearchQuery]],
        Awaitable[tuple[list[ReleaseResult], dict[str, str], list[dict[str, object]]]],
    ]
    BuildIssueQueriesFunc = Callable[..., list[SearchQuery]]
    BuildFallbackQueriesFunc = Callable[[IssueSearchTarget], list[SearchQuery]]
    SortBySourcePriorityFunc = Callable[
        [list[ReleaseResult], list[str]],
        list[ReleaseResult],
    ]
    FilterResultsFunc = Callable[
        [AsyncSession, list[ReleaseResult]],
        Awaitable[list[ReleaseResult]],
    ]
    SelectBestValidationFunc = Callable[..., ValidationResult | None]
    BuildSearchDetailsFunc = Callable[..., dict[str, object]]
    LogTypeDetectionFunc = Callable[
        [list[ValidationResult], list[ValidationResult], IssueType, str, float],
        None,
    ]


async def search_issue_target(
    session: AsyncSession,
    target: IssueSearchTarget,
    *,
    mode: IssueSearchMode = "deep",
    indexer_configs: dict[int, IndexerConfig] | None = None,
    eval_kwargs: SearchEvalKwargs | None = None,
    validator_kwargs: ValidatorKwargs | None = None,
    source_priority: list[str] | None = None,
    auto_fallback: bool = False,
    force_generic: bool = False,
    raw_results_override: list[ReleaseResult] | None = None,
    query_count_override: int | None = None,
    used_fallback_override: bool = False,
    build_issue_queries_func: BuildIssueQueriesFunc,
    build_fallback_queries_func: BuildFallbackQueriesFunc,
    run_query_batch_func: Callable[..., Awaitable[list[ReleaseResult]]],
    sort_by_source_priority_func: SortBySourcePriorityFunc,
    filter_results_func: FilterResultsFunc,
    validator_factory: type[ReleaseValidator],
    select_best_validation_func: SelectBestValidationFunc,
    build_search_details_func: BuildSearchDetailsFunc,
    log_type_detection_func: LogTypeDetectionFunc,
    log: structlog.stdlib.BoundLogger,
    session_lock: asyncio.Lock | None = None,
    run_query_batch_with_provenance_func: Callable[
        ...,
        Awaitable[tuple[list[ReleaseResult], dict[str, str], list[dict[str, object]]]],
    ]
    | None = None,
) -> IssueSearchOutcome:
    """Run the shared issue-search pipeline for one target."""
    started_at = time.monotonic()
    resolved_eval_kwargs: SearchEvalKwargs = eval_kwargs or {}
    resolved_validator_kwargs: ValidatorKwargs = validator_kwargs or {}

    if raw_results_override is not None:
        raw_results = raw_results_override
        query_count = 1 if query_count_override is None else query_count_override
        used_fallback = used_fallback_override
        query_provenance: dict[str, str] = {}
        query_diagnostics: list[dict[str, object]] = []
    else:
        queries = build_issue_queries_func(target, mode=mode, force_generic=force_generic)
        query_count = len(queries)
        if run_query_batch_with_provenance_func is not None:
            (
                raw_results,
                query_provenance,
                query_diagnostics,
            ) = await run_query_batch_with_provenance_func(
                queries,
                indexer_configs=indexer_configs,
            )
        else:
            raw_results = await run_query_batch_func(
                queries,
                indexer_configs=indexer_configs,
            )
            query_provenance = {}
            query_diagnostics = []
        used_fallback = False

        if mode == "deep" and auto_fallback and not force_generic and not raw_results:
            fallback_queries = build_fallback_queries_func(target)
            if fallback_queries:
                used_fallback = True
                query_count += len(fallback_queries)
                if run_query_batch_with_provenance_func is not None:
                    (
                        fallback_results,
                        fallback_provenance,
                        fallback_diagnostics,
                    ) = await run_query_batch_with_provenance_func(
                        fallback_queries,
                        indexer_configs=indexer_configs,
                    )
                    raw_results = fallback_results
                    query_provenance.update(fallback_provenance)
                    query_diagnostics.extend(fallback_diagnostics)
                else:
                    raw_results = await run_query_batch_func(
                        fallback_queries,
                        indexer_configs=indexer_configs,
                    )

    if source_priority:
        raw_results = sort_by_source_priority_func(raw_results, source_priority)

    if session_lock is None:
        filtered_results = await filter_results_func(session, raw_results)
    else:
        async with session_lock:
            filtered_results = await filter_results_func(session, raw_results)

    validator = validator_factory(**resolved_validator_kwargs)
    matched, rejected = validator.validate_all_results(
        filtered_results,
        wanted_series=target.series_title,
        wanted_issue=target.issue_number,
        wanted_year=target.search_year,
        year_context=target.year_context,
        wanted_issue_type=target.issue_type,
        alternate_names=target.alternate_names,
        wanted_issue_title=target.issue_title,
        wanted_series_issue_count=target.series_issue_count,
    )
    log_type_detection_func(
        matched,
        rejected,
        target.issue_type,
        target.series_title,
        target.issue_number,
    )

    best_validation = select_best_validation_func(
        matched,
        min_score=resolved_eval_kwargs.get("min_score", DEFAULT_MIN_SCORE),
        confidence_blend=resolved_eval_kwargs.get("confidence_blend", 0.40),
        min_size_mb=resolved_eval_kwargs.get("min_size_mb", DEFAULT_MIN_SIZE_MB),
        max_size_mb=resolved_eval_kwargs.get("max_size_mb", DEFAULT_MAX_SIZE_MB),
        preferred_format=resolved_eval_kwargs.get("preferred_format"),
        seeder_tiers=resolved_eval_kwargs.get("seeder_tiers"),
        score_weights=resolved_eval_kwargs.get("score_weights"),
        grabs_weight=resolved_eval_kwargs.get("grabs_weight", 0),
        pack_penalty=resolved_eval_kwargs.get("pack_penalty", -20),
        max_file_count=resolved_eval_kwargs.get("max_file_count", 5),
        preferred_language=resolved_eval_kwargs.get("preferred_language", "en"),
        digital_bonus=resolved_eval_kwargs.get("digital_bonus", 10),
        source_priority=source_priority,
    )
    best_release = best_validation.release if best_validation is not None else None

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    search_details = build_search_details_func(
        matched,
        rejected,
        search_time_ms=elapsed_ms,
        search_passes=2 if used_fallback else 1,
        query_provenance=query_provenance,
        query_diagnostics=query_diagnostics,
    )
    search_details["results_count"] = len(raw_results)
    search_details["filtered_results_count"] = len(filtered_results)
    search_details["query_count"] = query_count
    search_details["search_mode"] = mode
    search_details["used_fallback"] = used_fallback

    log.info(
        "issue_search_complete",
        issue_id=target.issue_id,
        series_id=target.series_id,
        mode=mode,
        query_count=query_count,
        raw_results=len(raw_results),
        filtered_results=len(filtered_results),
        matched=len(matched),
        rejected=len(rejected),
        used_fallback=used_fallback,
        elapsed_ms=elapsed_ms,
        best_match=search_details.get("best_match"),
        top_rejected=search_details.get("top_rejected", []),
        confidence_breakdown=search_details.get("confidence_breakdown", {}),
        slow_indexers=search_details.get("slow_indexers", []),
    )

    return IssueSearchOutcome(
        target=target,
        mode=mode,
        query_count=query_count,
        raw_results=raw_results,
        filtered_results=filtered_results,
        matched=matched,
        rejected=rejected,
        best_release=best_release,
        best_validation=best_validation,
        search_details=search_details,
        elapsed_ms=elapsed_ms,
        used_fallback=used_fallback,
    )
