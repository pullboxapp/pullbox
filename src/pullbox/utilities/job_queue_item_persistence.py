"""Processed item persistence orchestration for utility queue dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pullbox.utilities.base_executor import ApplyResult, ItemResult
from pullbox.utilities.job_queue_processed_result import (
    apply_processed_item_snapshot,
    build_processed_item_counter_delta,
    persist_processed_item_log_entries,
)
from pullbox.utilities.job_queue_runtime_logs import persist_runtime_log_entries
from pullbox.utilities.job_queue_runtime_state import apply_job_counter_snapshot
from pullbox.utilities.models import ItemState, UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pullbox.utilities.base_executor import (
        JobExecutor,
        JobRunSummary,
        ProcessedItem,
        RuntimeLogEntry,
    )


@dataclass(frozen=True, slots=True)
class ProcessedItemPersistenceResult:
    """Successful processed-item persistence outcome."""

    apply_result: ApplyResult
    completed_delta: int
    failed_delta: int
    skipped_delta: int
    warning_delta: int


@dataclass(frozen=True, slots=True)
class ProcessedItemFailurePersistenceResult:
    """Persistence-failure recovery outcome."""

    next_failed: int
    failure_warnings: int


def persist_post_commit_logs(
    session: Any,
    *,
    runtime_logs: Iterable[RuntimeLogEntry],
    persist_log: Callable[..., None],
    configured_level: str,
    job_id: str,
    item_id: str,
    file_path: str | None,
    processed: ProcessedItem,
) -> None:
    """Persist executor logs emitted after the item transaction commits."""
    persist_runtime_log_entries(
        session,
        runtime_logs=runtime_logs,
        persist_log=persist_log,
        configured_level=configured_level,
        job_id=job_id,
        item_id=item_id,
        default_file_path=file_path,
        worker_id=processed.worker_id,
        duration_ms=processed.duration_ms,
    )


async def persist_processed_item_result(
    session: Any,
    *,
    job_id: str,
    item_id: str,
    file_path: str | None,
    processed: ProcessedItem,
    payload_data: dict[str, Any],
    executor: JobExecutor,
    config: dict[str, Any],
    job_context: dict[str, Any] | None,
    summary: JobRunSummary,
    configured_level: str,
    persist_log: Callable[..., None],
    completed_at: str,
) -> ProcessedItemPersistenceResult | None:
    """Persist one successful worker result and preview updated job counters."""
    item = await session.get(UtilityJobItem, item_id)
    if item is None:
        return None

    if processed.result == ItemResult.CANCELLED:
        # An interrupted disposable stage is not a failed or skipped comic.
        # Retain original discovery/rollback evidence and do not apply executor side effects.
        item.state = ItemState.PENDING
        item.started_at = None
        item.completed_at = None
        item.worker_id = None
        persist_processed_item_log_entries(
            session,
            processed=processed,
            persist_log=persist_log,
            configured_level=configured_level,
            job_id=job_id,
            item_id=item_id,
            file_path=file_path,
        )
        return ProcessedItemPersistenceResult(ApplyResult(), 0, 0, 0, 0)

    apply_processed_item_snapshot(
        item,
        processed,
        completed_at=completed_at,
    )
    persist_processed_item_log_entries(
        session,
        processed=processed,
        persist_log=persist_log,
        configured_level=configured_level,
        job_id=job_id,
        item_id=item_id,
        file_path=file_path,
    )

    apply_result = await executor.apply_item_result(
        session,
        item,
        payload_data,
        processed,
        config,
        job_context,
        summary,
    )
    if apply_result.warning_message and not item.warning_message:
        item.warning_message = apply_result.warning_message
    (
        completed_delta,
        failed_delta,
        skipped_delta,
        warning_delta,
    ) = build_processed_item_counter_delta(
        processed,
        warning_increment=apply_result.warning_increment,
    )
    persist_runtime_log_entries(
        session,
        runtime_logs=apply_result.extra_logs,
        persist_log=persist_log,
        configured_level=configured_level,
        job_id=job_id,
        item_id=item_id,
        default_file_path=file_path,
        worker_id=processed.worker_id,
        duration_ms=processed.duration_ms,
    )

    current_job = await session.get(UtilityJob, job_id)
    if current_job is not None:
        apply_job_counter_snapshot(
            current_job,
            completed=summary.completed + completed_delta,
            failed=summary.failed + failed_delta,
            skipped=summary.skipped + skipped_delta,
            warnings=summary.warnings + warning_delta,
        )

    return ProcessedItemPersistenceResult(
        apply_result=apply_result,
        completed_delta=completed_delta,
        failed_delta=failed_delta,
        skipped_delta=skipped_delta,
        warning_delta=warning_delta,
    )


async def persist_processed_item_failure(
    session: Any,
    *,
    job_id: str,
    item_id: str,
    file_path: str | None,
    processed: ProcessedItem,
    persist_error: Exception,
    summary: JobRunSummary,
    configured_level: str,
    persist_log: Callable[..., None],
    completed_at: str,
) -> ProcessedItemFailurePersistenceResult | None:
    """Persist recovery state when a worker result could not be saved cleanly."""
    item = await session.get(UtilityJobItem, item_id)
    if item is None:
        return None

    apply_processed_item_snapshot(
        item,
        processed,
        state=ItemState.FAILED,
        error_message=f"Result persistence failed: {persist_error}",
        completed_at=completed_at,
    )

    failure_warnings = summary.warnings + (1 if processed.warning_message else 0)
    next_failed = summary.failed + 1
    persist_log(
        session,
        configured_level=configured_level,
        job_id=job_id,
        item_id=item_id,
        level="ERROR",
        message=f"Job result could not be persisted cleanly: {persist_error}",
        file_path=file_path,
        extra={"original_result": str(processed.result)},
        worker_id=processed.worker_id,
        duration_ms=processed.duration_ms,
    )

    current_job = await session.get(UtilityJob, job_id)
    if current_job is not None:
        apply_job_counter_snapshot(
            current_job,
            completed=summary.completed,
            failed=next_failed,
            skipped=summary.skipped,
            warnings=failure_warnings,
        )

    return ProcessedItemFailurePersistenceResult(
        next_failed=next_failed,
        failure_warnings=failure_warnings,
    )
