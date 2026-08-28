"""Search background task — finds and grabs wanted issues from indexers.

Runs on a configurable interval (default 6 hours).  For each wanted issue,
searches all enabled indexers, evaluates results, and routes the best match:
auto-grab (high confidence) or queue for user review (medium/low confidence).

Also provides ``search_series_issues()`` — a reusable helper that searches
all wanted issues for a single series.  Used by the SeriesAdded subscriber,
bulk search, and new-issue-sync flows.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import OperationalError

from pullbox.composition.events import build_domain_event_bus
from pullbox.config import get_settings
from pullbox.core.config_resolver import get_int_setting, load_system_config_values, parse_bool
from pullbox.core.log_deduper import log_deduped_warning
from pullbox.core.scheduler import (
    TaskExecutionResult,
    get_current_task_trigger_type,
    get_scheduler,
)
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.database import get_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.base import ProviderRegistry, ReleaseResult

from pullbox.composition.providers import build_registry
from pullbox.models.config import SystemConfig
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series
from pullbox.services import search_runtime as _search_runtime
from pullbox.services.airdcpp_automatic_search import attach_automatic_airdcpp_search
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_planner_service import plan_direct_acquisition
from pullbox.services.direct_discovery_retention import prune_unstarted_direct_discoveries
from pullbox.services.download_service import DownloadService
from pullbox.services.intervention_service import InterventionService
from pullbox.services.release_validator import (
    ReleaseValidator,
    ValidationResult,
)
from pullbox.services.search_acquisition_router import route_search_acquisition
from pullbox.services.search_service import (
    _TYPE_QUERY_KEYWORDS,
    DEFAULT_TYPE_THRESHOLDS,
    DEFAULT_WANTED_SEARCH_CONCURRENCY,
    IssueSearchOutcome,
    IssueSearchTarget,
    SearchRuntime,
    SearchService,
    build_eval_kwargs,
    load_series_wanted_search_targets,
    load_wanted_issue_search_targets,
)
from pullbox.services.search_source_selection import select_search_source
from pullbox.services.wanted_search_sweep import (
    WantedSearchSweepState,
    checkpoint_wanted_search_items,
    complete_wanted_search_batch,
    create_wanted_search_sweep,
    load_wanted_search_batch,
    load_wanted_search_sweep,
    mark_wanted_search_batch_running,
    pause_wanted_search_sweep,
    save_wanted_search_sweep,
)
from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner

logger = structlog.get_logger(__name__)
_ORIGINAL_SEARCH_SERVICE = SearchService
_ORIGINAL_SEARCH_FOR_ISSUE = SearchService.search_for_issue
_ORIGINAL_SEARCH_WANTED = SearchService.search_wanted
_SEARCH_TWO_PASS_CONFIG_KEY = "search_two_pass_enabled"
_SEARCH_LOG_RETENTION_CONFIG_KEY = "search_log_retention_days"
_SEARCH_WANTED_CURSOR_CONFIG_KEY = "search_wanted_cursor"
_SEARCH_WANTED_BATCH_LIMIT = 100
_DEFAULT_SEARCH_LOG_RETENTION_DAYS = 7


def _build_download_service(registry: ProviderRegistry) -> DownloadService:
    """Construct a task-local DownloadService while preserving patch seams."""
    return DownloadService(registry, build_domain_event_bus())


def _search_outcome_log_diagnostics(outcomes: list[IssueSearchOutcome]) -> dict[str, int]:
    """Aggregate lightweight query diagnostics for task completion logs."""

    slow_indexer_count = 0
    slowest_query_ms = 0
    for outcome in outcomes:
        slow_indexers = outcome.search_details.get("slow_indexers")
        if isinstance(slow_indexers, list):
            slow_indexer_count += len(slow_indexers)
        query_diagnostics = outcome.search_details.get("query_diagnostics")
        if not isinstance(query_diagnostics, list):
            continue
        for query_diag in query_diagnostics:
            if not isinstance(query_diag, dict):
                continue
            elapsed_ms = query_diag.get("elapsed_ms")
            if isinstance(elapsed_ms, int | float):
                slowest_query_ms = max(slowest_query_ms, int(elapsed_ms))
    return {
        "query_count": sum(outcome.query_count for outcome in outcomes),
        "slow_indexer_count": slow_indexer_count,
        "slowest_query_ms": slowest_query_ms,
    }


def _merge_search_log_details(
    *,
    existing_details: dict[str, object] | None,
    next_details: dict[str, object] | None,
    run_state: str,
    action_status: str | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    """Merge persisted search-log detail payloads while preserving launch metadata."""

    details = dict(existing_details or {})
    details.update(next_details or {})
    details["run_state"] = run_state
    if action_status:
        details["action_status"] = action_status
    if error_message:
        details["error"] = error_message
    elif action_status != "error":
        details.pop("error", None)
    return details


async def _persist_bulk_search_log(
    session: AsyncSession,
    *,
    target: IssueSearchTarget,
    pending_log_id: int | None,
    results_found: int,
    results_grabbed: int,
    results_queued: int,
    results_rejected: int,
    details: dict[str, object],
    best_confidence: str | None,
    action_status: str,
    run_state: str = "completed",
) -> None:
    """Create or update the bulk-search history row for a single issue."""

    search_log = await session.get(SearchLog, pending_log_id) if pending_log_id else None
    if search_log is None:
        search_log = SearchLog(
            issue_id=target.issue_id,
            series_title=target.series_title,
            issue_number=target.issue_number,
            search_type=SearchType.BULK,
        )
        session.add(search_log)

    search_log.results_found = results_found
    search_log.results_grabbed = results_grabbed
    search_log.results_queued = results_queued
    search_log.results_rejected = results_rejected
    search_log.details = _merge_search_log_details(
        existing_details=search_log.details or {},
        next_details=details,
        run_state=run_state,
        action_status=action_status,
    )
    search_log.best_confidence = best_confidence
    await session.commit()


async def _create_pending_wanted_search_logs(
    session: AsyncSession,
    targets: list[IssueSearchTarget],
    *,
    trigger_type: str,
) -> dict[int, int]:
    """Expose one running history row per wanted target before network work starts."""

    pending_logs: list[SearchLog] = []
    for target in targets:
        search_log = SearchLog(
            issue_id=target.issue_id,
            series_title=target.series_title,
            issue_number=target.issue_number,
            search_type=SearchType.AUTOMATED,
            results_found=0,
            results_grabbed=0,
            results_queued=0,
            results_rejected=0,
            details={
                "run_state": "running",
                "action_status": "searching",
                "task_id": "search_wanted",
                "trigger_type": trigger_type,
            },
        )
        session.add(search_log)
        pending_logs.append(search_log)

    await session.flush()
    pending_log_ids = {
        target.issue_id: search_log.id
        for target, search_log in zip(targets, pending_logs, strict=True)
    }
    await session.commit()
    return pending_log_ids


async def _ensure_pending_series_search_logs(
    session: AsyncSession,
    targets: list[IssueSearchTarget],
    *,
    series_id: int,
    existing_log_ids_by_issue: dict[int, int],
) -> dict[int, int]:
    """Expose missing bulk-search rows before a series search starts."""

    pending_log_ids = dict(existing_log_ids_by_issue)
    new_logs: list[tuple[IssueSearchTarget, SearchLog]] = []
    for target in targets:
        if target.issue_id in pending_log_ids:
            continue
        search_log = SearchLog(
            issue_id=target.issue_id,
            series_title=target.series_title,
            issue_number=target.issue_number,
            search_type=SearchType.BULK,
            results_found=0,
            results_grabbed=0,
            results_queued=0,
            results_rejected=0,
            details={
                "run_state": "running",
                "action_status": "searching",
                "task_id": f"search_series_{series_id}",
                "trigger_type": "automated",
            },
        )
        session.add(search_log)
        new_logs.append((target, search_log))

    if not new_logs:
        return pending_log_ids

    await session.flush()
    pending_log_ids.update({target.issue_id: search_log.id for target, search_log in new_logs})
    await session.commit()
    return pending_log_ids


async def _persist_wanted_search_outcome(
    session: AsyncSession,
    *,
    outcome: IssueSearchOutcome,
    pending_log_id: int | None,
    runtime: SearchRuntime,
    download_svc: DownloadService,
    intervention_svc: InterventionService,
) -> tuple[int, int, int]:
    """Route and persist one completed wanted-search outcome."""

    outcome = await attach_automatic_airdcpp_search(
        session,
        outcome,
        validator_kwargs=runtime.validator_kwargs,
    )
    target = outcome.target
    issue_grabbed = 0
    issue_queued = 0
    best_confidence: str | None = None
    direct_outcome = outcome.direct_outcome
    direct_results = (
        len(direct_outcome.matched) + len(direct_outcome.rejected) if direct_outcome else 0
    )
    dc_outcome = outcome.dc_outcome
    dc_results = len(dc_outcome.matched) + len(dc_outcome.rejected) if dc_outcome else 0
    total_results = len(outcome.raw_results) + direct_results + dc_results
    action_status = "no_results" if total_results == 0 else "no_match"
    try:
        search_log = await session.get(SearchLog, pending_log_id) if pending_log_id else None
        if search_log is None:
            search_log = SearchLog(
                issue_id=target.issue_id,
                series_title=target.series_title,
                issue_number=target.issue_number,
                search_type=SearchType.AUTOMATED,
            )
            session.add(search_log)
            await session.flush()

        routed = await route_search_acquisition(
            session,
            outcome=outcome,
            search_log_id=search_log.id,
            eval_kwargs=runtime.eval_kwargs,
            type_thresholds=runtime.type_thresholds,
            download_service=download_svc,
            intervention_service=intervention_svc,
            runner=(get_direct_acquisition_runner() if runtime.direct_providers else None),
            source_priority=runtime.source_priority,
            planner=plan_direct_acquisition,
        )
        issue_grabbed = routed.grabbed
        issue_queued = routed.queued
        best_confidence = routed.best_confidence
        action_status = (
            "no_results"
            if total_results == 0 and routed.action_status == "no_match"
            else routed.action_status
        )
        next_details = dict(outcome.search_details)
        if routed.notices:
            next_details["notices"] = list(routed.notices)
            logger.info(
                "search_source_fallback",
                issue_id=target.issue_id,
                notices=list(routed.notices),
            )

        search_log.results_found = total_results
        search_log.results_grabbed = issue_grabbed
        search_log.results_queued = issue_queued
        search_log.results_rejected = max(
            0,
            total_results - issue_grabbed - issue_queued,
        )
        search_log.details = _merge_search_log_details(
            existing_details=search_log.details or {},
            next_details=next_details,
            run_state="completed",
            action_status=action_status,
        )
        if routed.source_kind is not None:
            search_log.details["acquisition_method"] = routed.source_kind
        search_log.best_confidence = best_confidence
        await _save_search_wanted_cursor(session, target)
        await session.commit()
        return issue_grabbed, issue_queued, 0
    except Exception:
        await session.rollback()
        await _refresh_search_runtime_after_rollback(session, runtime)
        logger.exception("search_wanted_issue_failed", issue_id=target.issue_id)
        await _save_search_wanted_cursor(session, target)
        if pending_log_id is not None:
            await _complete_pending_search_logs(
                session,
                {target.issue_id: pending_log_id},
                action_status="error",
                run_state="failed",
                error_message="Search processing failed for this issue.",
            )
        else:
            session.add(
                SearchLog(
                    issue_id=target.issue_id,
                    series_title=target.series_title,
                    issue_number=target.issue_number,
                    search_type=SearchType.AUTOMATED,
                    results_found=total_results,
                    results_grabbed=0,
                    results_queued=0,
                    results_rejected=total_results,
                    details=_merge_search_log_details(
                        existing_details=None,
                        next_details=outcome.search_details,
                        run_state="failed",
                        action_status="error",
                        error_message="Search processing failed for this issue.",
                    ),
                    best_confidence=best_confidence,
                )
            )
            await session.commit()
        return 0, 0, 1


async def _persist_series_search_outcome(
    session: AsyncSession,
    *,
    log: structlog.stdlib.BoundLogger,
    primary_outcome: IssueSearchOutcome,
    fallback_outcome: IssueSearchOutcome | None,
    pending_log_id: int | None,
    runtime: SearchRuntime,
    download_svc: DownloadService,
    intervention_svc: InterventionService,
) -> tuple[int, int, int]:
    """Route and persist one completed series-search outcome."""

    primary_outcome = await attach_automatic_airdcpp_search(
        session,
        primary_outcome,
        validator_kwargs=runtime.validator_kwargs,
    )
    target = primary_outcome.target
    issue_log = log.bind(issue_id=target.issue_id, issue_number=target.issue_number)
    issue_log.info(
        "search_series_issue_results",
        indexer_results=len(primary_outcome.raw_results),
        search_pass=1,
        mode=primary_outcome.mode,
        query_count=primary_outcome.query_count,
        elapsed_ms=primary_outcome.elapsed_ms,
    )
    if fallback_outcome is not None:
        issue_log.info(
            "search_series_issue_results",
            indexer_results=len(fallback_outcome.raw_results),
            search_pass=2,
            mode=fallback_outcome.mode,
            query_count=fallback_outcome.query_count,
            elapsed_ms=fallback_outcome.elapsed_ms,
        )

    selected_outcome = primary_outcome
    selected_pass = 1
    primary_selection = select_search_source(
        primary_outcome,
        runtime.eval_kwargs,
        source_priority=runtime.source_priority,
    )
    fallback_selection = (
        select_search_source(
            fallback_outcome,
            runtime.eval_kwargs,
            source_priority=runtime.source_priority,
        )
        if fallback_outcome is not None
        else None
    )
    if (
        primary_selection is None
        and fallback_outcome is not None
        and fallback_selection is not None
    ):
        selected_outcome = fallback_outcome
        selected_pass = 2

    issue_grabbed = 0
    issue_queued = 0
    try:
        search_log = await session.get(SearchLog, pending_log_id) if pending_log_id else None
        if search_log is None:
            search_log = SearchLog(
                issue_id=target.issue_id,
                series_title=target.series_title,
                issue_number=target.issue_number,
                search_type=SearchType.BULK,
            )
            session.add(search_log)
            await session.flush()

        routed = await route_search_acquisition(
            session,
            outcome=selected_outcome,
            search_log_id=search_log.id,
            eval_kwargs=runtime.eval_kwargs,
            type_thresholds=runtime.type_thresholds,
            download_service=download_svc,
            intervention_service=intervention_svc,
            runner=(get_direct_acquisition_runner() if runtime.direct_providers else None),
            source_priority=runtime.source_priority,
            planner=plan_direct_acquisition,
        )
        issue_grabbed = routed.grabbed
        issue_queued = routed.queued
        if routed.source_kind is None:
            issue_log.info("search_series_issue_no_match", search_pass=selected_pass)
        elif routed.source_kind == "direct":
            issue_log.info(
                "search_series_issue_direct_routed",
                action_status=routed.action_status,
                confidence=routed.best_confidence,
                search_pass=selected_pass,
            )
        elif routed.source_kind == "dc":
            issue_log.info(
                "search_series_issue_dc_evaluated",
                action_status=routed.action_status,
                confidence=routed.best_confidence,
                search_pass=selected_pass,
            )
        else:
            selected = select_search_source(
                selected_outcome,
                runtime.eval_kwargs,
                source_priority=runtime.source_priority,
            )
            if selected is None:
                raise RuntimeError("Selected indexer result was not found.")
            if issue_grabbed:
                issue_log.info(
                    "search_series_issue_auto_grab",
                    best_title=selected.release.title,
                    best_indexer=selected.release.indexer_name,
                    confidence=selected.validation.confidence.value,
                )
            elif routed.action_status == "pending_exists":
                issue_log.info(
                    "search_series_issue_pending_exists",
                    best_title=selected.release.title,
                    search_pass=selected_pass,
                )
            elif issue_queued:
                issue_log.info(
                    "search_series_issue_queued",
                    best_title=selected.release.title,
                    confidence=selected.validation.confidence.value,
                    search_pass=selected_pass,
                )

        details = dict(selected_outcome.search_details)
        if routed.notices:
            details["notices"] = list(routed.notices)
            issue_log.info("search_source_fallback", notices=list(routed.notices))
        if fallback_outcome is not None:
            total_found = len(primary_outcome.raw_results) + len(fallback_outcome.raw_results)
            details["results_count"] = total_found
            details["search_passes"] = 2
            details["pass1_results_count"] = len(primary_outcome.raw_results)
            details["pass2_results_count"] = len(fallback_outcome.raw_results)
        else:
            results_count_value = details.get("results_count")
            total_found = (
                int(results_count_value)
                if isinstance(results_count_value, int | float | str)
                else len(selected_outcome.raw_results)
            )
            details.setdefault(
                "search_passes",
                selected_outcome.search_details.get("search_passes", 1),
            )
        direct_outcome = selected_outcome.direct_outcome
        direct_results = (
            len(direct_outcome.matched) + len(direct_outcome.rejected) if direct_outcome else 0
        )
        dc_outcome = selected_outcome.dc_outcome
        dc_results = len(dc_outcome.matched) + len(dc_outcome.rejected) if dc_outcome else 0
        total_found += direct_results + dc_results
        details["direct_results_count"] = direct_results
        details["dc_results_count"] = dc_results
        if routed.source_kind is not None:
            details["acquisition_method"] = routed.source_kind

        await _persist_bulk_search_log(
            session,
            target=target,
            pending_log_id=pending_log_id,
            results_found=total_found,
            results_grabbed=issue_grabbed,
            results_queued=issue_queued,
            results_rejected=max(0, total_found - issue_grabbed - issue_queued),
            details=details,
            best_confidence=routed.best_confidence,
            action_status=(
                "no_results"
                if total_found == 0 and routed.action_status == "no_match"
                else routed.action_status
            ),
        )
        return issue_grabbed, issue_queued, 0
    except Exception:
        await session.rollback()
        await _refresh_search_runtime_after_rollback(session, runtime)
        if pending_log_id is not None:
            await _complete_pending_bulk_search_logs(
                session,
                {target.issue_id: pending_log_id},
                action_status="error",
                run_state="failed",
                error_message="Search processing failed for this issue.",
            )
        issue_log.exception("search_series_issue_failed", search_pass=selected_pass)
        return 0, 0, 1


async def _refresh_search_runtime_after_rollback(
    session: AsyncSession,
    runtime: SearchRuntime,
) -> None:
    """Reload ORM-backed runtime state expired by a routing rollback."""
    for indexer_config in runtime.indexer_configs.values():
        await session.refresh(indexer_config)


async def _complete_pending_search_logs(
    session: AsyncSession,
    pending_log_ids_by_issue: dict[int, int],
    *,
    action_status: str,
    run_state: str = "completed",
    error_message: str | None = None,
) -> None:
    """Resolve still-running search rows after a cancelled or failed launch path."""

    touched = False
    for pending_log_id in pending_log_ids_by_issue.values():
        search_log = await session.get(SearchLog, pending_log_id)
        if search_log is None:
            continue
        search_log.details = _merge_search_log_details(
            existing_details=search_log.details or {},
            next_details=None,
            run_state=run_state,
            action_status=action_status,
            error_message=error_message,
        )
        touched = True

    if touched:
        await session.commit()


async def _complete_pending_bulk_search_logs(
    session: AsyncSession,
    pending_log_ids_by_issue: dict[int, int],
    *,
    action_status: str,
    run_state: str = "completed",
    error_message: str | None = None,
) -> None:
    """Resolve still-running bulk-search rows while preserving the existing seam."""

    await _complete_pending_search_logs(
        session,
        pending_log_ids_by_issue,
        action_status=action_status,
        run_state=run_state,
        error_message=error_message,
    )


async def _build_task_search_runtime(
    session: AsyncSession,
    *,
    include_download_clients: bool = True,
) -> SearchRuntime | None:
    """Build task runtime state using the task module's registry patch point."""
    return await _search_runtime.build_search_runtime(
        session,
        include_download_clients=include_download_clients,
        registry_builder=build_registry,
        default_type_thresholds=DEFAULT_TYPE_THRESHOLDS,
        eval_kwargs_builder=build_eval_kwargs,
        include_direct_providers=True,
    )


def _is_mocked_search_service(search_svc: object) -> bool:
    """Return True when the task search service has been replaced by a test double."""

    if not isinstance(search_svc, _ORIGINAL_SEARCH_SERVICE):
        return True

    service_type = type(search_svc)
    return (
        service_type.search_for_issue is not _ORIGINAL_SEARCH_FOR_ISSUE
        or service_type.search_wanted is not _ORIGINAL_SEARCH_WANTED
    )


async def _load_mocked_two_pass_enabled(
    session: AsyncSession,
    runtime: SearchRuntime,
) -> bool:
    """Resolve the two-pass toggle for legacy mocked search-task tests."""
    configs = await load_system_config_values(session, (_SEARCH_TWO_PASS_CONFIG_KEY,))
    raw_value = configs.get(_SEARCH_TWO_PASS_CONFIG_KEY)
    if raw_value is None:
        return runtime.two_pass_enabled
    return parse_bool(raw_value)


async def _load_search_log_retention_days(session: AsyncSession) -> int:
    """Resolve search-log retention in days."""
    configs = await load_system_config_values(session, (_SEARCH_LOG_RETENTION_CONFIG_KEY,))
    return get_int_setting(
        configs,
        _SEARCH_LOG_RETENTION_CONFIG_KEY,
        _DEFAULT_SEARCH_LOG_RETENTION_DAYS,
    )


def _search_wanted_cursor_from_target(target: IssueSearchTarget) -> tuple[int, float, int]:
    """Return the stable global wanted-sweep cursor tuple for a target."""
    return (target.series_id, target.issue_number, target.issue_id)


def _parse_search_wanted_cursor(value: str | None) -> tuple[int, float, int] | None:
    """Parse the persisted wanted-sweep cursor, ignoring stale malformed values."""
    if not value:
        return None
    try:
        raw = json.loads(value)
        if (
            isinstance(raw, list | tuple)
            and len(raw) == 3
            and isinstance(raw[0], int | float | str)
            and isinstance(raw[1], int | float | str)
            and isinstance(raw[2], int | float | str)
        ):
            return (int(raw[0]), float(raw[1]), int(raw[2]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


async def _load_search_wanted_cursor(session: AsyncSession) -> tuple[int, float, int] | None:
    """Load the last attempted global wanted-search cursor."""
    row = await session.get(SystemConfig, _SEARCH_WANTED_CURSOR_CONFIG_KEY)
    return _parse_search_wanted_cursor(row.value if row is not None else None)


async def _save_search_wanted_cursor(
    session: AsyncSession,
    target: IssueSearchTarget,
) -> None:
    """Persist the last attempted global wanted-search target."""
    value = json.dumps(list(_search_wanted_cursor_from_target(target)))
    row = await session.get(SystemConfig, _SEARCH_WANTED_CURSOR_CONFIG_KEY)
    if row is None:
        session.add(
            SystemConfig(
                key=_SEARCH_WANTED_CURSOR_CONFIG_KEY,
                value=value,
                value_type="string",
            )
        )
        return
    row.value = value
    row.value_type = "string"


async def _load_rotated_wanted_issue_targets(
    session: AsyncSession,
    *,
    limit: int = _SEARCH_WANTED_BATCH_LIMIT,
) -> list[IssueSearchTarget]:
    """Load a fair global wanted-search batch, continuing after the saved cursor."""
    cursor = await _load_search_wanted_cursor(session)
    if cursor is None:
        return await load_wanted_issue_search_targets(session, limit=limit)

    targets = await load_wanted_issue_search_targets(session, limit=limit, after=cursor)
    if len(targets) >= limit:
        return targets

    seen_issue_ids = {target.issue_id for target in targets}
    wrapped = await load_wanted_issue_search_targets(session, limit=limit - len(targets))
    targets.extend(target for target in wrapped if target.issue_id not in seen_issue_ids)
    return targets


async def _build_mocked_issue_outcome(
    session: AsyncSession,
    search_svc: object,
    target: IssueSearchTarget,
    runtime: SearchRuntime,
    *,
    force_generic: bool = False,
) -> IssueSearchOutcome:
    """Adapt old mocked search task tests onto the shared outcome structure."""

    raw_results = await search_svc.search_for_issue(  # type: ignore[attr-defined]
        session,
        target.issue_id,
        force_generic=force_generic,
        source_priority=runtime.source_priority,
    )
    filtered_results = await BlocklistService.filter_results(session, raw_results)
    best = None
    if filtered_results:
        best = search_svc.evaluate_results(  # type: ignore[attr-defined]
            filtered_results,
            wanted_series=target.series_title,
            wanted_issue=target.issue_number,
            wanted_year=target.search_year,
            wanted_issue_type=target.issue_type,
            wanted_issue_title=target.issue_title,
            alternate_names=target.alternate_names,
            source_priority=runtime.source_priority,
            **runtime.eval_kwargs,
        )

    matched: list[ValidationResult] = []
    rejected: list[ValidationResult] = []
    best_validation = None
    if best is not None:
        validator = ReleaseValidator(**runtime.validator_kwargs)
        matched = validator.validate_results(
            [best],
            wanted_series=target.series_title,
            wanted_issue=target.issue_number,
            wanted_year=target.search_year,
            wanted_issue_type=target.issue_type,
            alternate_names=target.alternate_names,
            wanted_issue_title=target.issue_title,
        )
        if matched:
            best_validation = matched[0]

    return IssueSearchOutcome(
        target=target,
        mode="fast" if force_generic else "deep",
        query_count=1,
        raw_results=raw_results,
        filtered_results=filtered_results,
        matched=matched,
        rejected=rejected,
        best_release=best if best_validation is not None else None,
        best_validation=best_validation,
        search_details={
            "results_count": len(raw_results),
            "filtered_results_count": len(filtered_results),
            "query_count": 1,
            "search_mode": "fast" if force_generic else "deep",
            "used_fallback": False,
        },
        elapsed_ms=0,
    )


async def _build_mocked_wanted_outcome(
    session: AsyncSession,
    search_svc: object,
    target: IssueSearchTarget,
    runtime: SearchRuntime,
    raw_results: list[ReleaseResult],
) -> IssueSearchOutcome:
    """Adapt mocked wanted-search maps into the shared outcome structure."""

    filtered_results = await BlocklistService.filter_results(session, raw_results)
    best = None
    search_error: str | None = None
    if filtered_results:
        try:
            best = search_svc.evaluate_results(  # type: ignore[attr-defined]
                filtered_results,
                wanted_series=target.series_title,
                wanted_issue=target.issue_number,
                wanted_year=target.search_year,
                wanted_issue_type=target.issue_type,
                wanted_issue_title=target.issue_title,
                alternate_names=target.alternate_names,
                source_priority=runtime.source_priority,
                **runtime.eval_kwargs,
            )
        except Exception as exc:  # pragma: no cover - exercised by integration tests
            search_error = str(exc)
            logger.exception(
                "search_wanted_issue_evaluation_failed",
                issue_id=target.issue_id,
                series_id=target.series_id,
                series_title=target.series_title,
                issue_number=target.issue_number,
            )

    matched: list[ValidationResult] = []
    rejected: list[ValidationResult] = []
    best_validation = None
    if best is not None:
        validator = ReleaseValidator(**runtime.validator_kwargs)
        matched = validator.validate_results(
            [best],
            wanted_series=target.series_title,
            wanted_issue=target.issue_number,
            wanted_year=target.search_year,
            wanted_issue_type=target.issue_type,
            alternate_names=target.alternate_names,
            wanted_issue_title=target.issue_title,
        )
        if matched:
            best_validation = matched[0]

    return IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=raw_results,
        filtered_results=filtered_results,
        matched=matched,
        rejected=rejected,
        best_release=best if best_validation is not None else None,
        best_validation=best_validation,
        search_details={
            "results_count": len(raw_results),
            "filtered_results_count": len(filtered_results),
            "query_count": 1,
            "search_mode": "fast",
            "used_fallback": False,
            "error": search_error,
        },
        elapsed_ms=0,
    )


async def search_series_issues(
    series_id: int,
    *,
    pending_log_ids_by_issue: dict[int, int] | None = None,
) -> dict[str, int]:
    """Search indexers for all wanted issues of a single series.

    Obtains its own DB session so it can be called from event subscribers
    and background tasks without sharing caller state.

    Returns:
        Dict with ``wanted``, ``sent``, and ``queued`` counts.
    """
    log = logger.bind(series_id=series_id)
    log.info("search_series_issues_start")
    remaining_pending_log_ids = dict(pending_log_ids_by_issue or {})

    factory = get_session_factory()
    async with factory() as session:
        try:
            preload_started_at = time.monotonic()
            runtime = await _build_task_search_runtime(
                session,
                include_download_clients=True,
            )
            if runtime is None:
                if remaining_pending_log_ids:
                    await _complete_pending_bulk_search_logs(
                        session,
                        remaining_pending_log_ids,
                        action_status="no_indexers",
                    )
                log.info("search_series_issues_no_indexers")
                return {"wanted": 0, "sent": 0, "queued": 0}

            series = await session.get(Series, series_id)
            if not series:
                if remaining_pending_log_ids:
                    await _complete_pending_bulk_search_logs(
                        session,
                        remaining_pending_log_ids,
                        action_status="series_not_found",
                    )
                log.warning("search_series_issues_not_found")
                return {"wanted": 0, "sent": 0, "queued": 0}

            targets = await load_series_wanted_search_targets(session, series_id)
            if not targets:
                if remaining_pending_log_ids:
                    await _complete_pending_bulk_search_logs(
                        session,
                        remaining_pending_log_ids,
                        action_status="no_wanted",
                    )
                log.info("search_series_issues_no_wanted")
                return {"wanted": 0, "sent": 0, "queued": 0}

            remaining_pending_log_ids = await _ensure_pending_series_search_logs(
                session,
                targets,
                series_id=series_id,
                existing_log_ids_by_issue=remaining_pending_log_ids,
            )
            preload_ms = int((time.monotonic() - preload_started_at) * 1000)
            log.info(
                "search_series_issues_config",
                series=str(series.title),
                year=series.year_start,
                wanted_count=len(targets),
                preload_ms=preload_ms,
                quick_first_two_pass=runtime.two_pass_enabled,
                concurrency=DEFAULT_WANTED_SEARCH_CONCURRENCY,
            )

            search_started_at = time.monotonic()
            pass1_outcomes: list[IssueSearchOutcome] = []
            pass2_outcomes: list[IssueSearchOutcome] = []
            processed_issue_ids: set[int] = set()
            processed_outcomes: list[IssueSearchOutcome] = []
            download_svc = _build_download_service(runtime.registry)
            intervention_svc = InterventionService(download_svc)
            sent = 0
            queued = 0
            failed = 0
            routing_ms = 0

            async def _process_outcome_pair(
                primary_outcome: IssueSearchOutcome,
                fallback_outcome: IssueSearchOutcome | None = None,
            ) -> None:
                nonlocal failed, queued, routing_ms, sent
                if runtime is None:  # Defensive guard for the retry-assigned closure.
                    msg = "Search runtime became unavailable during outcome processing"
                    raise RuntimeError(msg)
                issue_id = primary_outcome.target.issue_id
                if issue_id in processed_issue_ids:
                    return

                routing_started_at = time.monotonic()
                # Preserve provider health even if downstream routing rolls back.
                await session.commit()
                issue_sent, issue_queued, issue_failed = await _persist_series_search_outcome(
                    session,
                    log=log,
                    primary_outcome=primary_outcome,
                    fallback_outcome=fallback_outcome,
                    pending_log_id=remaining_pending_log_ids.get(issue_id),
                    runtime=runtime,
                    download_svc=download_svc,
                    intervention_svc=intervention_svc,
                )
                sent += issue_sent
                queued += issue_queued
                failed += issue_failed
                routing_ms += int((time.monotonic() - routing_started_at) * 1000)
                processed_issue_ids.add(issue_id)
                remaining_pending_log_ids.pop(issue_id, None)
                processed_outcomes.append(primary_outcome)
                if fallback_outcome is not None:
                    processed_outcomes.append(fallback_outcome)

            async def _process_outcome(outcome: IssueSearchOutcome) -> None:
                await _process_outcome_pair(outcome)

            for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
                try:
                    if attempt > 1:
                        retry_runtime = await _build_task_search_runtime(
                            session,
                            include_download_clients=True,
                        )
                        if retry_runtime is None:
                            msg = "Search runtime became unavailable during lock retry"
                            raise RuntimeError(msg)
                        runtime = retry_runtime

                    search_svc = SearchService(
                        runtime.registry,
                        failure_threshold=runtime.failure_threshold,
                        direct_providers=runtime.direct_providers,
                    )
                    if _is_mocked_search_service(search_svc):
                        two_pass_enabled = await _load_mocked_two_pass_enabled(session, runtime)
                        pass1_outcomes = [
                            await _build_mocked_issue_outcome(
                                session,
                                search_svc,
                                target,
                                runtime,
                            )
                            for target in targets
                        ]
                        pass2_targets = [
                            outcome.target
                            for outcome in pass1_outcomes
                            if outcome.best_validation is None
                            and outcome.target.issue_type.value in _TYPE_QUERY_KEYWORDS
                            and two_pass_enabled
                        ]
                        pass2_outcomes = [
                            await _build_mocked_issue_outcome(
                                session,
                                search_svc,
                                target,
                                runtime,
                                force_generic=True,
                            )
                            for target in pass2_targets
                        ]
                    else:
                        pass1_outcomes = await search_svc.search_targets_quick_first(
                            session,
                            targets,
                            indexer_configs=runtime.indexer_configs,
                            eval_kwargs=runtime.eval_kwargs,
                            validator_kwargs=runtime.validator_kwargs,
                            source_priority=runtime.source_priority,
                            enable_deep_fallback=runtime.two_pass_enabled,
                            concurrency=DEFAULT_WANTED_SEARCH_CONCURRENCY,
                            on_outcome=_process_outcome,
                        )
                        pass2_outcomes = []

                    # Persist indexer health immediately after network fan-out.
                    await session.commit()
                    break
                except OperationalError as exc:
                    await session.rollback()
                    if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                        raise
                    delay_seconds = sqlite_lock_retry_delay(attempt)
                    log.warning(
                        "search_series_retrying_after_sqlite_lock",
                        attempt=attempt,
                        max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                        delay_seconds=delay_seconds,
                        stage="search_fanout_persist",
                    )
                    await asyncio.sleep(delay_seconds)
            search_fanout_ms = int((time.monotonic() - search_started_at) * 1000)

            pass2_by_issue = {outcome.target.issue_id: outcome for outcome in pass2_outcomes}
            for pass1_outcome in pass1_outcomes:
                await _process_outcome_pair(
                    pass1_outcome,
                    pass2_by_issue.get(pass1_outcome.target.issue_id),
                )

            if remaining_pending_log_ids:
                await _complete_pending_bulk_search_logs(
                    session,
                    remaining_pending_log_ids,
                    action_status="error",
                    run_state="failed",
                    error_message="Search returned no outcome for this issue.",
                )
                failed += len(remaining_pending_log_ids)
                remaining_pending_log_ids.clear()

            log.info(
                "search_series_issues_complete",
                wanted=len(targets),
                sent=sent,
                queued=queued,
                preload_ms=preload_ms,
                search_fanout_ms=search_fanout_ms,
                routing_ms=routing_ms,
                failed=failed,
                **_search_outcome_log_diagnostics(processed_outcomes),
            )
            return {"wanted": len(targets), "sent": sent, "queued": queued}
        except Exception:
            await session.rollback()
            if remaining_pending_log_ids:
                await _complete_pending_bulk_search_logs(
                    session,
                    remaining_pending_log_ids,
                    action_status="error",
                    run_state="failed",
                    error_message="Search failed before completion.",
                )
            log.exception("search_series_issues_failed")
            return {"wanted": 0, "sent": 0, "queued": 0}


async def search_wanted() -> TaskExecutionResult:
    """Run one bounded batch in a durable complete wanted-issue sweep."""
    factory = get_session_factory()
    trigger_type = get_current_task_trigger_type()
    runtime: SearchRuntime | None = None
    sweep: WantedSearchSweepState | None = None
    targets: list[IssueSearchTarget] = []
    pending_log_ids: dict[int, int] = {}
    wanted_outcomes: list[IssueSearchOutcome] = []
    preload_ms = 0
    search_fanout_ms = 0
    routing_ms = 0

    async with factory() as session:
        for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
            try:
                preload_started_at = time.monotonic()
                local_runtime = await _build_task_search_runtime(
                    session,
                    include_download_clients=True,
                )
                if local_runtime is None:
                    log_deduped_warning(
                        logger,
                        "search_wanted_missing_indexers",
                        key="search_wanted_missing_indexers",
                        action_required="Enable at least one indexer to search wanted issues.",
                    )
                    return TaskExecutionResult()

                now = datetime.now(UTC)
                sweep = await load_wanted_search_sweep(session)
                if sweep is None or sweep.state in {"completed", "failed"}:
                    sweep = await create_wanted_search_sweep(
                        session,
                        trigger_type=trigger_type,
                        now=now,
                    )
                elif (
                    sweep.state == "waiting"
                    and sweep.next_batch_at is not None
                    and sweep.next_batch_at > now
                    and trigger_type != "manual"
                ):
                    _schedule_wanted_sweep_continuation(sweep)
                    await session.commit()
                    return TaskExecutionResult(status="waiting")
                else:
                    sweep = mark_wanted_search_batch_running(sweep)
                    await save_wanted_search_sweep(session, sweep)

                batch = await load_wanted_search_batch(
                    session,
                    sweep,
                    limit=_SEARCH_WANTED_BATCH_LIMIT,
                )
                targets = batch.targets
                if batch.skipped_issue_ids:
                    sweep = checkpoint_wanted_search_items(
                        sweep,
                        issue_ids=batch.skipped_issue_ids,
                        searched_count=0,
                        sent=0,
                        queued=0,
                        failed=0,
                    )
                    await save_wanted_search_sweep(session, sweep)
                    await session.commit()
                if not batch.issue_ids:
                    completed = complete_wanted_search_batch(
                        sweep,
                        issue_ids=[],
                        searched_count=0,
                        sent=0,
                        queued=0,
                        failed=0,
                        now=now,
                    )
                    await save_wanted_search_sweep(session, completed)
                    await session.commit()
                    await _sync_wanted_sweep_schedule(session, completed)
                    logger.debug("search_wanted_no_targets")
                    return TaskExecutionResult()
                if not targets:
                    completed = complete_wanted_search_batch(
                        sweep,
                        issue_ids=[],
                        searched_count=0,
                        sent=0,
                        queued=0,
                        failed=0,
                        now=now,
                    )
                    await save_wanted_search_sweep(session, completed)
                    await session.commit()
                    await _sync_wanted_sweep_schedule(session, completed)
                    return TaskExecutionResult(
                        status="completed" if completed.state == "completed" else "waiting"
                    )
                preload_ms = int((time.monotonic() - preload_started_at) * 1000)
                pending_log_ids = await _create_pending_wanted_search_logs(
                    session,
                    targets,
                    trigger_type=trigger_type,
                )
                runtime = local_runtime
                break
            except OperationalError as exc:
                await session.rollback()
                if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    raise
                delay_seconds = sqlite_lock_retry_delay(attempt)
                logger.warning(
                    "search_wanted_retrying_after_sqlite_lock",
                    attempt=attempt,
                    max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                    delay_seconds=delay_seconds,
                    stage="pending_history_persist",
                )
                await asyncio.sleep(delay_seconds)
            except Exception:
                await session.rollback()
                raise

        if runtime is None:
            return TaskExecutionResult()

        search_svc = SearchService(
            runtime.registry,
            failure_threshold=runtime.failure_threshold,
            ignore_indexer_backoff=False,
            direct_providers=runtime.direct_providers,
        )
        download_svc = _build_download_service(runtime.registry)
        intervention_svc = InterventionService(download_svc)
        processed_issue_ids: set[int] = set()
        sent = 0
        queued = 0
        failed = 0

        async def _process_outcome(outcome: IssueSearchOutcome) -> None:
            nonlocal failed, queued, routing_ms, sent, sweep
            issue_id = outcome.target.issue_id
            if issue_id in processed_issue_ids:
                return

            routing_started_at = time.monotonic()
            issue_sent, issue_queued, issue_failed = await _persist_wanted_search_outcome(
                session,
                outcome=outcome,
                pending_log_id=pending_log_ids.get(issue_id),
                runtime=runtime,
                download_svc=download_svc,
                intervention_svc=intervention_svc,
            )
            sent += issue_sent
            queued += issue_queued
            failed += issue_failed
            routing_ms += int((time.monotonic() - routing_started_at) * 1000)
            processed_issue_ids.add(issue_id)
            wanted_outcomes.append(outcome)
            if sweep is None:
                raise RuntimeError("Wanted search sweep state was not initialized.")
            sweep = checkpoint_wanted_search_items(
                sweep,
                issue_ids=[issue_id],
                searched_count=1,
                sent=issue_sent,
                queued=issue_queued,
                failed=issue_failed,
            )
            await save_wanted_search_sweep(session, sweep)
            await session.commit()

        try:
            search_started_at = time.monotonic()
            if _is_mocked_search_service(search_svc):
                results_map = await search_svc.search_wanted(
                    session,
                    indexer_configs=runtime.indexer_configs,
                )
                local_outcomes = [
                    await _build_mocked_wanted_outcome(
                        session,
                        search_svc,
                        target,
                        runtime,
                        results_map.get(target.issue_id, []),
                    )
                    for target in targets
                ]
            else:
                local_outcomes = await search_svc.search_targets_quick_first(
                    session,
                    targets,
                    indexer_configs=runtime.indexer_configs,
                    eval_kwargs=runtime.eval_kwargs,
                    validator_kwargs=runtime.validator_kwargs,
                    source_priority=runtime.source_priority,
                    enable_deep_fallback=runtime.two_pass_enabled,
                    concurrency=DEFAULT_WANTED_SEARCH_CONCURRENCY,
                    on_outcome=_process_outcome,
                )

            # Test doubles and compatibility implementations may not invoke the
            # callback. Finalize their returned outcomes without duplicating real ones.
            for outcome in local_outcomes:
                await _process_outcome(outcome)

            remaining_log_ids = {
                issue_id: log_id
                for issue_id, log_id in pending_log_ids.items()
                if issue_id not in processed_issue_ids
            }
            if remaining_log_ids:
                await _save_search_wanted_cursor(session, targets[-1])
                await _complete_pending_search_logs(
                    session,
                    remaining_log_ids,
                    action_status="error",
                    run_state="failed",
                    error_message="Search returned no outcome for this issue.",
                )
                failed += len(remaining_log_ids)
                if sweep is None:
                    raise RuntimeError("Wanted search sweep state was not initialized.")
                missing_issue_ids = list(remaining_log_ids)
                sweep = checkpoint_wanted_search_items(
                    sweep,
                    issue_ids=missing_issue_ids,
                    searched_count=len(missing_issue_ids),
                    sent=0,
                    queued=0,
                    failed=len(missing_issue_ids),
                )
                await save_wanted_search_sweep(session, sweep)
                await session.commit()

            total_elapsed_ms = int((time.monotonic() - search_started_at) * 1000)
            search_fanout_ms = max(0, total_elapsed_ms - routing_ms)

            if sweep is None:
                raise RuntimeError("Wanted search sweep state was not initialized.")
            completed_sweep = complete_wanted_search_batch(
                sweep,
                issue_ids=[],
                searched_count=0,
                sent=0,
                queued=0,
                failed=0,
                now=datetime.now(UTC),
            )
            await save_wanted_search_sweep(session, completed_sweep)
            await session.commit()
            await _sync_wanted_sweep_schedule(session, completed_sweep)
            log_event = (
                "search_wanted_complete"
                if completed_sweep.state == "completed"
                else "search_wanted_batch_complete"
            )
            logger.info(
                log_event,
                sent=sent,
                queued=queued,
                failed=failed,
                total=len(wanted_outcomes),
                sweep_attempted=completed_sweep.attempted_count,
                sweep_total=completed_sweep.total_targets,
                sweep_remaining=completed_sweep.remaining_count,
                batch_number=completed_sweep.batch_number,
                next_batch_at=(
                    completed_sweep.next_batch_at.isoformat()
                    if completed_sweep.next_batch_at
                    else None
                ),
                preload_ms=preload_ms,
                search_fanout_ms=search_fanout_ms,
                routing_ms=routing_ms,
                **_search_outcome_log_diagnostics(wanted_outcomes),
            )
            return TaskExecutionResult(
                status="completed" if completed_sweep.state == "completed" else "waiting"
            )
        except asyncio.CancelledError:
            await session.rollback()
            remaining_log_ids = {
                issue_id: log_id
                for issue_id, log_id in pending_log_ids.items()
                if issue_id not in processed_issue_ids
            }
            await _complete_pending_search_logs(
                session,
                remaining_log_ids,
                action_status="cancelled",
                run_state="cancelled",
                error_message="Search was cancelled before completion.",
            )
            if sweep is not None:
                paused = pause_wanted_search_sweep(
                    sweep,
                    message="Interrupted; retrying the current batch later",
                )
                await save_wanted_search_sweep(session, paused)
                await session.commit()
                _schedule_wanted_sweep_continuation(paused)
            raise
        except Exception:
            await session.rollback()
            remaining_log_ids = {
                issue_id: log_id
                for issue_id, log_id in pending_log_ids.items()
                if issue_id not in processed_issue_ids
            }
            await _complete_pending_search_logs(
                session,
                remaining_log_ids,
                action_status="error",
                run_state="failed",
                error_message="Search failed before completion.",
            )
            if sweep is not None:
                paused = pause_wanted_search_sweep(
                    sweep,
                    message="Batch failed; retrying later",
                )
                await save_wanted_search_sweep(session, paused)
                await session.commit()
                _schedule_wanted_sweep_continuation(paused)
            logger.exception("search_wanted_failed")
            raise


async def recover_wanted_search_sweep_schedule() -> bool:
    """Restore an interrupted continuation after process startup."""
    factory = get_session_factory()
    async with factory() as session:
        sweep = await load_wanted_search_sweep(session)
        if (
            sweep is None
            or sweep.state not in {"running", "waiting"}
            or not sweep.pending_issue_ids
        ):
            return False
        if sweep.state == "running":
            sweep = pause_wanted_search_sweep(
                sweep,
                now=datetime.now(UTC) - timedelta(hours=1),
                message="Resuming interrupted batch",
            )
            await save_wanted_search_sweep(session, sweep)
            await session.commit()
        _schedule_wanted_sweep_continuation(sweep)
        return True


def _schedule_wanted_sweep_continuation(sweep: WantedSearchSweepState) -> None:
    run_at = sweep.next_batch_at or datetime.now(UTC)
    if run_at <= datetime.now(UTC):
        run_at = datetime.now(UTC) + timedelta(seconds=1)
    get_scheduler().schedule_task_continuation(
        "search_wanted",
        run_at=run_at,
        interval_seconds=3600,
    )


async def _sync_wanted_sweep_schedule(
    session: AsyncSession,
    sweep: WantedSearchSweepState,
) -> None:
    scheduler = get_scheduler()
    if sweep.state == "waiting":
        _schedule_wanted_sweep_continuation(sweep)
        return
    scheduler.clear_task_continuation("search_wanted")
    configs = await load_system_config_values(session, ("search_interval_hours",))
    interval_hours = get_int_setting(
        configs,
        "search_interval_hours",
        get_settings().search_interval_hours,
    )
    scheduler.delay_task_next_run(
        "search_wanted",
        run_at=datetime.now(UTC) + timedelta(hours=max(1, interval_hours)),
    )


async def purge_search_logs() -> None:
    """Delete search log entries older than the configured retention period."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete, select

    factory = get_session_factory()

    async with factory() as session:
        try:
            retention_days = await _load_search_log_retention_days(session)

            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            old_search_log_ids = select(SearchLog.id).where(SearchLog.created_at < cutoff)
            await prune_unstarted_direct_discoveries(session, old_search_log_ids)
            result = await session.execute(delete(SearchLog).where(SearchLog.created_at < cutoff))
            pruned = result.rowcount  # type: ignore[attr-defined]
            await session.commit()

            if pruned:
                logger.info(
                    "purge_search_logs_complete",
                    pruned=pruned,
                    retention_days=retention_days,
                )
        except Exception:
            await session.rollback()
            raise
