"""Search service — orchestrates indexer searches for wanted issues."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import structlog

from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import IssueType
from pullbox.providers import base as _provider_base
from pullbox.services import search_evaluation as _search_evaluation
from pullbox.services import search_indexers as _search_indexers
from pullbox.services import search_issue_runner as _search_issue_runner
from pullbox.services import search_query_helpers as _search_query_helpers
from pullbox.services import search_runtime as _search_runtime
from pullbox.services import search_scoring as _search_scoring
from pullbox.services import search_targets as _search_targets
from pullbox.services import search_types as _search_types
from pullbox.services.direct_search_coordinator import (
    DirectSearchOutcome,
    DirectSearchProvider,
    search_direct_issue_target,
)
from pullbox.services.release_validator import (
    ReleaseValidator,
    ValidationResult,
)
from pullbox.services.search_statistics import SearchStats as SearchStats
from pullbox.services.search_statistics import get_search_stats as get_search_stats
from pullbox.services.search_targets import (
    IssueSearchTarget as IssueSearchTarget,
)
from pullbox.services.search_targets import (
    load_issue_search_target as load_issue_search_target,
)
from pullbox.services.search_targets import (
    load_series_wanted_search_targets as load_series_wanted_search_targets,
)
from pullbox.services.search_targets import (
    load_wanted_issue_search_targets as load_wanted_issue_search_targets,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.providers.base import Indexer, ProviderRegistry
    from pullbox.services.search_indexers import IndexerSearchAttempt
    from pullbox.services.search_types import SearchEvalKwargs, ValidatorKwargs

    DirectSearchFunc = Callable[..., Awaitable[DirectSearchOutcome]]

logger = structlog.get_logger(__name__)

_ORIGINAL_SEARCH_FOR_ISSUE = None

# ── Scoring Constants ─────────────────────────────────────────────────
# These defaults will eventually come from SystemConfig / user settings.

DEFAULT_MIN_SIZE_MB = _search_scoring.DEFAULT_MIN_SIZE_MB
DEFAULT_MAX_SIZE_MB = _search_scoring.DEFAULT_MAX_SIZE_MB
DEFAULT_MIN_SCORE = _search_evaluation.DEFAULT_MIN_SCORE
PREFERRED_FORMATS = _search_scoring.PREFERRED_FORMATS
EVAL_CONFIG_KEYS = _search_scoring.EVAL_CONFIG_KEYS

score_release = _search_scoring.score_release
build_eval_kwargs = _search_scoring.build_eval_kwargs
_sort_by_source_priority = _search_scoring._sort_by_source_priority
_score_indexer_priority = _search_scoring._score_indexer_priority
_score_age = _search_scoring._score_age
_score_size = _search_scoring._score_size
_score_format = _search_scoring._score_format
_score_category = _search_scoring._score_category
_score_seeders = _search_scoring._score_seeders
_score_grabs = _search_scoring._score_grabs
_score_pack_penalty = _search_scoring._score_pack_penalty
_score_language = _search_scoring._score_language
_score_digital_bonus = _search_scoring._score_digital_bonus

# Default Newznab/Torznab categories for comic searches.
# 7020 = EBook, 7030 = Comics.  Only these two subcategories — the
# parent 7000 (all Books) is intentionally excluded to avoid flooding
# results with novels and general ebooks.
DEFAULT_COMIC_CATEGORIES = _search_query_helpers.DEFAULT_COMIC_CATEGORIES
# Keep bulk wanted searches conservative so one series-wide run does not
# overwhelm the aggregate Prowlarr/indexer fan-out path.
DEFAULT_WANTED_SEARCH_CONCURRENCY = 1
SEARCH_QUERY_CACHE_TTL_SECONDS = 15
MAX_SHARED_SEARCH_QUERY_CACHE_ENTRIES = 256
_SHARED_QUERY_CACHE: dict[tuple[object, ...], tuple[float, list[ReleaseResult]]] = {}
_SHARED_QUERY_INFLIGHT: dict[tuple[object, ...], asyncio.Task[list[ReleaseResult]]] = {}

_COLLECTION_TYPES = _search_query_helpers._COLLECTION_TYPES
_TYPE_QUERY_KEYWORDS = _search_query_helpers._TYPE_QUERY_KEYWORDS
_is_better_release = _search_query_helpers._is_better_release
_extract_subtitle = _search_query_helpers._extract_subtitle
_is_comic_category = _search_query_helpers._is_comic_category
_sanitize_query = _search_query_helpers._sanitize_query
_build_type_queries = _search_query_helpers._build_type_queries
_dedupe_release_results = _search_query_helpers._dedupe_release_results

_score_validation_result = _search_evaluation._score_validation_result
_select_best_validation = _search_evaluation._select_best_validation
DEFAULT_INDEXER_FAILURE_THRESHOLD = _search_indexers.DEFAULT_INDEXER_FAILURE_THRESHOLD
INDEXER_BACKOFF_SECONDS = _search_indexers.INDEXER_BACKOFF_SECONDS
calculate_backoff = _search_indexers.calculate_backoff
SearchRuntime = _search_runtime.SearchRuntime
ReleaseResult = _provider_base.ReleaseResult
SearchQuery = _provider_base.SearchQuery
IssueSearchOutcome = _search_targets.IssueSearchOutcome


def build_search_details(
    matched: list[ValidationResult],
    rejected: list[ValidationResult],
    *,
    search_time_ms: int | None = None,
    search_passes: int = 1,
    query_provenance: dict[str, str] | None = None,
    query_diagnostics: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Compatibility facade for search-log detail shaping."""
    return _search_evaluation.build_search_details(
        matched,
        rejected,
        search_time_ms=search_time_ms,
        search_passes=search_passes,
        query_provenance=query_provenance,
        query_diagnostics=query_diagnostics,
    )


def log_type_detection(
    matched: list[ValidationResult],
    rejected: list[ValidationResult],
    wanted_type: IssueType,
    wanted_series: str,
    wanted_issue: float,
) -> None:
    """Compatibility facade for structured search type-detection logging."""
    _search_evaluation.log_type_detection(
        matched,
        rejected,
        wanted_type,
        wanted_series,
        wanted_issue,
        log=logger,
    )


IssueSearchMode = _search_types.IssueSearchMode


DEFAULT_TYPE_THRESHOLDS = _search_runtime.DEFAULT_TYPE_THRESHOLDS


def _direct_search_diagnostics(outcome: DirectSearchOutcome) -> dict[str, object]:
    """Return the bounded direct-provider detail persisted in search history."""
    return {
        "providers_searched": outcome.providers_searched,
        "elapsed_ms": outcome.elapsed_ms,
        "failures": [
            {
                "provider_identity": failure.provider_identity,
                "provider_name": failure.provider_name,
                "code": failure.code,
                "retryable": failure.retryable,
            }
            for failure in outcome.failures
        ],
        "resolver_attempts": [
            {
                "resolver_id": attempt.resolver_id,
                "resolver_name": attempt.resolver_name,
                "resolver_kind": attempt.resolver_kind.value,
                "attempt": attempt.attempt,
                "total": attempt.total,
                "scope": attempt.scope,
            }
            for attempt in outcome.resolver_attempts
        ],
    }


should_auto_grab = _search_runtime.should_auto_grab


def _format_query_label(query: SearchQuery) -> str:
    """Return the human query string used for diagnostics."""

    if query.issue_number is None:
        return query.series_title
    issue = format_issue_number(query.issue_number)
    return f"{query.series_title} {issue}"


def summarize_search_pass(outcome: IssueSearchOutcome) -> dict[str, object]:
    """Build a compact summary for the first pass of a quick-first search."""

    return {
        "query_count": outcome.query_count,
        "results_count": len(outcome.raw_results),
        "matched_count": len(outcome.matched),
        "rejected_count": len(outcome.rejected),
        "elapsed_ms": outcome.elapsed_ms,
    }


_summarize_search_pass = summarize_search_pass


def _query_cache_key(
    query: SearchQuery,
    *,
    registry_id: int,
    indexer_configs: dict[int, IndexerConfig] | None,
    failure_threshold: int,
) -> tuple[object, ...]:
    """Build a cache key for identical query/indexer work in one service context."""

    config_signature = _indexer_config_cache_signature(indexer_configs)
    registry_scope: tuple[object, ...]
    if config_signature is None:
        registry_scope = ("registry", registry_id)
    else:
        registry_scope = ("configs", config_signature)
    categories = tuple(query.categories or ())
    return (
        registry_scope,
        query.series_title,
        query.issue_number,
        query.year,
        query.issue_type,
        categories,
        failure_threshold,
    )


def _indexer_config_cache_signature(
    indexer_configs: dict[int, IndexerConfig] | None,
) -> tuple[tuple[object, ...], ...] | None:
    """Return a stable short-TTL cache signature for real configured indexers."""

    if not indexer_configs:
        return None

    signature: list[tuple[object, ...]] = []
    for config_id, cfg in sorted(indexer_configs.items()):
        disabled_until = getattr(cfg, "disabled_until", None)
        if disabled_until is not None and hasattr(disabled_until, "isoformat"):
            disabled_value = disabled_until.isoformat()
        else:
            disabled_value = str(disabled_until) if disabled_until is not None else None
        signature.append(
            (
                config_id,
                str(getattr(cfg, "name", "") or ""),
                str(getattr(cfg, "indexer_type", "") or ""),
                str(getattr(cfg, "url", "") or ""),
                bool(getattr(cfg, "enabled", True)),
                int(getattr(cfg, "priority", 50) or 50),
                str(getattr(cfg, "categories", "") or ""),
                disabled_value,
            )
        )
    return tuple(signature)


def _remember_query_cache_result(
    cache_store: dict[tuple[object, ...], tuple[float, list[ReleaseResult]]],
    cache_key: tuple[object, ...],
    results: list[ReleaseResult],
    *,
    max_entries: int | None = None,
) -> None:
    """Store a short-lived query result and keep the shared cache bounded."""

    cache_store[cache_key] = (time.monotonic(), list(results))
    if max_entries is None:
        return
    while len(cache_store) > max_entries:
        cache_store.pop(next(iter(cache_store)))


def _record_cache_timing(
    timing_collector: list[dict[str, object]] | None,
    *,
    query: SearchQuery,
    status: str,
    result_count: int,
) -> None:
    """Record cache/coalescing events in the same diagnostics channel as indexer timing."""

    if timing_collector is None:
        return
    timing_collector.append(
        {
            "query": query.series_title,
            "indexer": "cache",
            "status": status,
            "elapsed_ms": 0,
            "raw_count": result_count,
            "result_count": result_count,
            "filtered_count": 0,
            "categories": query.categories,
        }
    )


class SearchService:
    """Searches indexers for wanted issues and ranks results.

    Args:
        registry: Provider registry for accessing indexers.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        failure_threshold: int = DEFAULT_INDEXER_FAILURE_THRESHOLD,
        *,
        ignore_indexer_backoff: bool = False,
        direct_providers: Sequence[DirectSearchProvider] = (),
        direct_search_func: DirectSearchFunc = search_direct_issue_target,
    ) -> None:
        self._registry = registry
        self._failure_threshold = failure_threshold
        self._ignore_indexer_backoff = ignore_indexer_backoff
        self._direct_providers = tuple(direct_providers)
        self._direct_search_func = direct_search_func
        self._direct_search_tasks: dict[int, asyncio.Task[DirectSearchOutcome]] = {}
        self._indexer_failure_cohort: set[int] = set()
        self._query_cache: dict[tuple[object, ...], tuple[float, list[ReleaseResult]]] = {}
        self._query_inflight: dict[tuple[object, ...], asyncio.Task[list[ReleaseResult]]] = {}

    def _build_issue_queries(
        self,
        target: IssueSearchTarget,
        *,
        mode: IssueSearchMode,
        force_generic: bool = False,
    ) -> list[SearchQuery]:
        """Compatibility facade for issue-target query construction."""
        return _search_query_helpers.build_issue_queries(
            target,
            mode=mode,
            force_generic=force_generic,
        )

    def _build_auto_fallback_queries(self, target: IssueSearchTarget) -> list[SearchQuery]:
        """Compatibility facade for auto-fallback query construction."""
        return _search_query_helpers.build_auto_fallback_queries(target)

    async def _run_query_batch(
        self,
        queries: list[SearchQuery],
        *,
        indexer_configs: dict[int, IndexerConfig] | None = None,
    ) -> list[ReleaseResult]:
        """Run one batch of search queries and deduplicate the combined results."""

        per_query_results = await asyncio.gather(
            *[self._search_indexers(query, indexer_configs=indexer_configs) for query in queries]
        )
        combined: list[ReleaseResult] = []
        for query_results in per_query_results:
            combined.extend(query_results)
        return _dedupe_release_results(combined)

    async def _search_indexers_for_query(
        self,
        query: SearchQuery,
        *,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        timing_collector: list[dict[str, object]] | None = None,
    ) -> list[ReleaseResult]:
        """Call the indexer seam while preserving older test monkeypatches."""

        search_func = self._search_indexers
        accepts_timing = False
        if timing_collector is not None:
            try:
                parameters = inspect.signature(search_func).parameters
                accepts_timing = "timing_collector" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
            except (TypeError, ValueError):
                accepts_timing = False
        if accepts_timing:
            return await search_func(
                query,
                indexer_configs=indexer_configs,
                timing_collector=timing_collector,
            )
        return await search_func(query, indexer_configs=indexer_configs)

    async def _run_query_batch_with_provenance(
        self,
        queries: list[SearchQuery],
        *,
        indexer_configs: dict[int, IndexerConfig] | None = None,
    ) -> tuple[list[ReleaseResult], dict[str, str], list[dict[str, object]]]:
        """Run a query batch and remember which generated query surfaced each result."""

        async def _search_query(
            query: SearchQuery,
        ) -> tuple[SearchQuery, list[ReleaseResult], int, list[dict[str, object]]]:
            started_at = time.monotonic()
            indexer_timings: list[dict[str, object]] = []
            results = await self._search_indexers_for_query(
                query,
                indexer_configs=indexer_configs,
                timing_collector=indexer_timings,
            )
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return query, results, elapsed_ms, indexer_timings

        per_query_results = await asyncio.gather(*[_search_query(query) for query in queries])
        combined: list[ReleaseResult] = []
        provenance: dict[str, str] = {}
        query_diagnostics: list[dict[str, object]] = []
        for query, query_results, elapsed_ms, indexer_timings in per_query_results:
            query_label = _format_query_label(query)
            for result in query_results:
                provenance.setdefault(
                    _search_evaluation.release_provenance_key(result),
                    query_label,
                )
            query_diagnostics.append(
                {
                    "query": query_label,
                    "elapsed_ms": elapsed_ms,
                    "result_count": len(query_results),
                    "indexers": indexer_timings,
                }
            )
            combined.extend(query_results)

        return _dedupe_release_results(combined), provenance, query_diagnostics

    async def search_issue_target(
        self,
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
        session_lock: asyncio.Lock | None = None,
    ) -> IssueSearchOutcome:
        """Run the shared issue-search pipeline for one target."""

        raw_results_override: list[ReleaseResult] | None = None
        query_count_override: int | None = None
        used_fallback_override = False
        search_for_issue_impl = type(self).search_for_issue
        if (
            _ORIGINAL_SEARCH_FOR_ISSUE is not None
            and search_for_issue_impl is not _ORIGINAL_SEARCH_FOR_ISSUE
        ):
            raw_results_override = await search_for_issue_impl(
                self,
                session,
                target.issue_id,
                indexer_configs=indexer_configs,
                force_generic=force_generic,
                auto_fallback=auto_fallback,
                source_priority=source_priority,
            )
            query_count_override = 1

        from pullbox.services.blocklist_service import BlocklistService

        direct_task = self._direct_search_tasks.get(target.issue_id)
        if direct_task is None and self._direct_providers:
            direct_task = asyncio.create_task(
                self._search_direct_safely(
                    target,
                    validator_kwargs=validator_kwargs,
                ),
                name=f"direct-search-issue-{target.issue_id}",
            )
            self._direct_search_tasks[target.issue_id] = direct_task

        indexer_search = _search_issue_runner.search_issue_target(
            session,
            target,
            mode=mode,
            indexer_configs=indexer_configs,
            eval_kwargs=eval_kwargs,
            validator_kwargs=validator_kwargs,
            source_priority=source_priority,
            auto_fallback=auto_fallback,
            force_generic=force_generic,
            raw_results_override=raw_results_override,
            query_count_override=query_count_override,
            used_fallback_override=used_fallback_override,
            build_issue_queries_func=self._build_issue_queries,
            build_fallback_queries_func=self._build_auto_fallback_queries,
            run_query_batch_func=self._run_query_batch,
            run_query_batch_with_provenance_func=self._run_query_batch_with_provenance,
            sort_by_source_priority_func=_sort_by_source_priority,
            filter_results_func=BlocklistService.filter_results,
            validator_factory=ReleaseValidator,
            select_best_validation_func=_select_best_validation,
            build_search_details_func=build_search_details,
            log_type_detection_func=log_type_detection,
            log=logger,
            session_lock=session_lock,
        )
        if direct_task is None:
            return await indexer_search
        indexer_outcome, direct_outcome = await asyncio.gather(indexer_search, direct_task)
        direct_results = [
            item.release for item in (*direct_outcome.matched, *direct_outcome.rejected)
        ]
        kept_direct_results = await BlocklistService.filter_results(session, direct_results)
        if len(kept_direct_results) != len(direct_results):
            kept_direct_ids = {id(result) for result in kept_direct_results}
            direct_outcome = replace(
                direct_outcome,
                matched=tuple(
                    item for item in direct_outcome.matched if id(item.release) in kept_direct_ids
                ),
                rejected=tuple(
                    item for item in direct_outcome.rejected if id(item.release) in kept_direct_ids
                ),
            )
        indexer_outcome.search_details["direct_search"] = _direct_search_diagnostics(direct_outcome)
        return replace(indexer_outcome, direct_outcome=direct_outcome)

    async def _search_direct_safely(
        self,
        target: IssueSearchTarget,
        *,
        validator_kwargs: ValidatorKwargs | None,
    ) -> DirectSearchOutcome:
        """Keep a direct coordinator failure from changing legacy results."""
        try:
            return await self._direct_search_func(
                target,
                self._direct_providers,
                validator_kwargs=validator_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "direct_issue_search_failed",
                issue_id=target.issue_id,
                failure_code="direct_search_coordinator_failed",
            )
            return DirectSearchOutcome((), (), (), len(self._direct_providers), 0)

    async def search_issue_target_quick_first(
        self,
        session: AsyncSession,
        target: IssueSearchTarget,
        *,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        eval_kwargs: SearchEvalKwargs | None = None,
        validator_kwargs: ValidatorKwargs | None = None,
        source_priority: list[str] | None = None,
        enable_deep_fallback: bool = True,
        session_lock: asyncio.Lock | None = None,
    ) -> IssueSearchOutcome:
        """Run the shared quick-first issue search strategy.

        This keeps automated bulk searches aligned with issue-scoped manual search:
        try the exact fast path first, then fall back to deep search only when
        the exact pass does not validate a match.
        """

        fast_outcome = await self.search_issue_target(
            session,
            target,
            mode="fast",
            indexer_configs=indexer_configs,
            eval_kwargs=eval_kwargs,
            validator_kwargs=validator_kwargs,
            source_priority=source_priority,
            session_lock=session_lock,
        )
        direct_matched = bool(fast_outcome.direct_outcome and fast_outcome.direct_outcome.matched)
        if fast_outcome.matched or direct_matched or not enable_deep_fallback:
            fast_outcome.search_details["search_strategy"] = (
                "quick_first" if fast_outcome.matched else "quick_first_single_pass"
            )
            return fast_outcome

        fast_summary = _summarize_search_pass(fast_outcome)
        deep_outcome = await self.search_issue_target(
            session,
            target,
            mode="deep",
            indexer_configs=indexer_configs,
            eval_kwargs=eval_kwargs,
            validator_kwargs=validator_kwargs,
            source_priority=source_priority,
            auto_fallback=True,
            session_lock=session_lock,
        )
        deep_outcome.search_details["search_strategy"] = "quick_first_deep_fallback"
        deep_outcome.search_details["fast_search"] = fast_summary
        return deep_outcome

    async def search_targets_quick_first(
        self,
        session: AsyncSession,
        targets: list[IssueSearchTarget],
        *,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        eval_kwargs: SearchEvalKwargs | None = None,
        validator_kwargs: ValidatorKwargs | None = None,
        source_priority: list[str] | None = None,
        enable_deep_fallback: bool = True,
        concurrency: int = DEFAULT_WANTED_SEARCH_CONCURRENCY,
        on_outcome: _search_targets.SearchOutcomeCallback | None = None,
    ) -> list[IssueSearchOutcome]:
        """Search a batch of issue targets with the shared quick-first strategy."""

        async def _search_target(
            session: AsyncSession,
            target: IssueSearchTarget,
            *,
            mode: IssueSearchMode = "fast",
            indexer_configs: dict[int, IndexerConfig] | None = None,
            eval_kwargs: SearchEvalKwargs | None = None,
            validator_kwargs: ValidatorKwargs | None = None,
            source_priority: list[str] | None = None,
            auto_fallback: bool = False,
            force_generic: bool = False,
            session_lock: asyncio.Lock | None = None,
        ) -> IssueSearchOutcome:
            del mode, auto_fallback, force_generic
            return await self.search_issue_target_quick_first(
                session,
                target,
                indexer_configs=indexer_configs,
                eval_kwargs=eval_kwargs,
                validator_kwargs=validator_kwargs,
                source_priority=source_priority,
                enable_deep_fallback=enable_deep_fallback,
                session_lock=session_lock,
            )

        return await _search_targets.search_issue_targets(
            session,
            targets,
            mode="fast",
            search_issue_target_func=_search_target,
            indexer_configs=indexer_configs,
            eval_kwargs=eval_kwargs,
            validator_kwargs=validator_kwargs,
            source_priority=source_priority,
            concurrency=concurrency,
            on_outcome=on_outcome,
        )

    async def search_targets(
        self,
        session: AsyncSession,
        targets: list[IssueSearchTarget],
        *,
        mode: IssueSearchMode,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        eval_kwargs: SearchEvalKwargs | None = None,
        validator_kwargs: ValidatorKwargs | None = None,
        source_priority: list[str] | None = None,
        auto_fallback: bool = False,
        force_generic: bool = False,
        concurrency: int = DEFAULT_WANTED_SEARCH_CONCURRENCY,
    ) -> list[IssueSearchOutcome]:
        """Search a batch of issue targets with bounded concurrency."""

        return await _search_targets.search_issue_targets(
            session,
            targets,
            mode=mode,
            search_issue_target_func=self.search_issue_target,
            indexer_configs=indexer_configs,
            eval_kwargs=eval_kwargs,
            validator_kwargs=validator_kwargs,
            source_priority=source_priority,
            auto_fallback=auto_fallback,
            force_generic=force_generic,
            concurrency=concurrency,
        )

    async def search_for_issue(
        self,
        session: AsyncSession,
        issue_id: int,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        *,
        force_generic: bool = False,
        auto_fallback: bool = False,
        source_priority: list[str] | None = None,
    ) -> list[ReleaseResult]:
        """Search all enabled indexers for a specific issue."""

        from pullbox.core.exceptions import NotFoundError

        target = await load_issue_search_target(session, issue_id)
        if target is None:
            raise NotFoundError("Issue", issue_id)

        outcome = await self.search_issue_target(
            session,
            target,
            mode="deep",
            indexer_configs=indexer_configs,
            source_priority=source_priority,
            auto_fallback=auto_fallback,
            force_generic=force_generic,
        )
        return outcome.raw_results

    async def search_wanted(
        self,
        session: AsyncSession,
        limit: int = 50,
        indexer_configs: dict[int, IndexerConfig] | None = None,
    ) -> dict[int, list[ReleaseResult]]:
        """Search for all wanted issues across all indexers."""

        wanted = await load_wanted_issue_search_targets(session, limit=limit)
        concurrency = min(DEFAULT_WANTED_SEARCH_CONCURRENCY, len(wanted)) or 1

        logger.info(
            "search_wanted_start",
            issue_count=len(wanted),
            concurrency=concurrency,
        )

        outcomes = await self.search_targets_quick_first(
            session,
            wanted,
            indexer_configs=indexer_configs,
            enable_deep_fallback=True,
            concurrency=concurrency,
        )

        results_map = {
            outcome.target.issue_id: outcome.raw_results
            for outcome in outcomes
            if outcome.raw_results
        }

        logger.info(
            "search_wanted_complete",
            searched=len(outcomes),
            with_results=len(results_map),
            concurrency=concurrency,
        )
        return results_map

    @staticmethod
    def evaluate_results(
        results: list[ReleaseResult],
        indexer_priority: int | None = None,
        min_score: float = DEFAULT_MIN_SCORE,
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
    ) -> ReleaseResult | None:
        """Compatibility facade for release validation and result ranking."""
        return _search_evaluation.evaluate_results(
            results,
            indexer_priority,
            min_score,
            wanted_series=wanted_series,
            wanted_issue=wanted_issue,
            wanted_year=wanted_year,
            wanted_issue_type=wanted_issue_type,
            wanted_issue_title=wanted_issue_title,
            alternate_names=alternate_names,
            ignore_words=ignore_words,
            fuzzy_high_threshold=fuzzy_high_threshold,
            fuzzy_low_threshold=fuzzy_low_threshold,
            year_tolerance=year_tolerance,
            warn_issue_mb=warn_issue_mb,
            warn_collection_mb=warn_collection_mb,
            min_size_mb=min_size_mb,
            max_size_mb=max_size_mb,
            preferred_format=preferred_format,
            seeder_tiers=seeder_tiers,
            score_weights=score_weights,
            confidence_blend=confidence_blend,
            grabs_weight=grabs_weight,
            pack_penalty=pack_penalty,
            max_file_count=max_file_count,
            preferred_language=preferred_language,
            digital_bonus=digital_bonus,
            source_priority=source_priority,
            log_type_detection_func=log_type_detection,
            log=logger,
        )

    async def search(
        self,
        query: SearchQuery,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        source_priority: list[str] | None = None,
    ) -> list[ReleaseResult]:
        """Search all registered indexers for a given query."""
        results = await self._search_indexers(query, indexer_configs=indexer_configs)
        if source_priority:
            results = _sort_by_source_priority(results, source_priority)
        return results

    async def _search_indexers(
        self,
        query: SearchQuery,
        indexer_configs: dict[int, IndexerConfig] | None = None,
        timing_collector: list[dict[str, object]] | None = None,
    ) -> list[ReleaseResult]:
        """Compatibility facade for indexer fan-out and category filtering."""
        if self._ignore_indexer_backoff:
            return await _search_indexers.search_indexers(
                self._registry,
                query,
                indexer_configs=indexer_configs,
                failure_threshold=self._failure_threshold,
                search_single_indexer_func=self._search_single_indexer,
                log=logger,
                timing_collector=timing_collector,
                ignore_backoff=True,
            )

        cache_key = _query_cache_key(
            query,
            registry_id=id(self._registry),
            indexer_configs=indexer_configs,
            failure_threshold=self._failure_threshold,
        )
        now = time.monotonic()
        use_shared_cache = bool(indexer_configs)
        query_cache = _SHARED_QUERY_CACHE if use_shared_cache else self._query_cache
        query_inflight = _SHARED_QUERY_INFLIGHT if use_shared_cache else self._query_inflight

        cached = query_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_results = cached
            if now - cached_at < SEARCH_QUERY_CACHE_TTL_SECONDS:
                _record_cache_timing(
                    timing_collector,
                    query=query,
                    status="cache_hit",
                    result_count=len(cached_results),
                )
                return list(cached_results)
            query_cache.pop(cache_key, None)

        inflight = query_inflight.get(cache_key)
        if inflight is not None:
            results = await asyncio.shield(inflight) if use_shared_cache else await inflight
            _record_cache_timing(
                timing_collector,
                query=query,
                status="coalesced",
                result_count=len(results),
            )
            return list(results)

        async def _search_uncached() -> list[ReleaseResult]:
            return await _search_indexers.search_indexers(
                self._registry,
                query,
                indexer_configs=indexer_configs,
                failure_threshold=self._failure_threshold,
                search_single_indexer_func=self._search_single_indexer,
                log=logger,
                timing_collector=timing_collector,
            )

        task = asyncio.create_task(_search_uncached())
        query_inflight[cache_key] = task
        if use_shared_cache:

            def _complete_shared_query(done: asyncio.Task[list[ReleaseResult]]) -> None:
                if query_inflight.get(cache_key) is done:
                    query_inflight.pop(cache_key, None)
                if done.cancelled():
                    return
                try:
                    results = done.result()
                except Exception:
                    return
                _remember_query_cache_result(
                    query_cache,
                    cache_key,
                    results,
                    max_entries=MAX_SHARED_SEARCH_QUERY_CACHE_ENTRIES,
                )

            task.add_done_callback(_complete_shared_query)
            results = await asyncio.shield(task)
            return list(results)

        try:
            results = await task
        finally:
            query_inflight.pop(cache_key, None)
        _remember_query_cache_result(query_cache, cache_key, results)
        return list(results)

    async def _search_single_indexer(
        self,
        indexer: Indexer,
        query: SearchQuery,
        cfg: IndexerConfig | None = None,
        failure_threshold: int = DEFAULT_INDEXER_FAILURE_THRESHOLD,
    ) -> IndexerSearchAttempt:
        """Compatibility facade for a single indexer search attempt."""
        return await _search_indexers.search_single_indexer(
            indexer,
            query,
            cfg,
            failure_threshold,
            failure_cohort=self._indexer_failure_cohort,
        )


_ORIGINAL_SEARCH_FOR_ISSUE = SearchService.search_for_issue


async def build_search_runtime(
    session: AsyncSession,
    *,
    include_download_clients: bool = True,
    allow_empty_registry: bool = False,
    include_direct_providers: bool = False,
) -> SearchRuntime | None:
    """Compatibility facade for shared search runtime construction."""
    return await _search_runtime.build_search_runtime(
        session,
        include_download_clients=include_download_clients,
        allow_empty_registry=allow_empty_registry,
        include_direct_providers=include_direct_providers,
    )
