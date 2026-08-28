"""Batch-result orchestration for utility queue dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.utilities.job_queue_batch_failure import (
    build_batch_dispatch_failure_item,
    persist_batch_dispatch_failure_item,
    remaining_batch_item_ids,
)
from pullbox.utilities.job_queue_batch_state import (
    lease_dispatch_batch,
    prepare_batch_checkpoint,
)
from pullbox.utilities.job_queue_item_persistence import (
    persist_post_commit_logs,
    persist_processed_item_failure,
    persist_processed_item_result,
)
from pullbox.utilities.job_queue_items import build_batch_payloads
from pullbox.utilities.job_queue_worker_runtime import build_processed_item_stream

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pullbox.utilities.base_executor import JobExecutor, JobRunSummary
    from pullbox.utilities.models import UtilityJobItem


async def process_dispatch_batch(
    *,
    session_factory: Callable[[], Any],
    job_id: str,
    job_type: str,
    executor: JobExecutor,
    config: dict[str, Any],
    job_context: dict[str, Any] | None,
    summary: JobRunSummary,
    utility_log_level: str,
    batch_items: list[UtilityJobItem],
    worker_pool: Any,
    persist_log: Callable[..., None],
    logger: Any,
    timestamp_factory: Callable[[], str],
) -> None:
    """Process one in-progress utility batch and persist all batch outcomes."""
    batch_payload_bundle = build_batch_payloads(batch_items)
    batch_payloads = batch_payload_bundle.payloads
    batch_items_by_id = batch_payload_bundle.items_by_id
    batch_payloads_by_id = batch_payload_bundle.payloads_by_id

    seen_item_ids: set[str] = set()
    try:
        processed_stream = build_processed_item_stream(
            job_type=job_type,
            worker_pool=worker_pool,
            payloads=batch_payloads,
            executor=executor,
            config=config,
            job_context=job_context,
        )
        async for processed in processed_stream:
            matched_db_item = batch_items_by_id.get(processed.item_id)
            payload_data = batch_payloads_by_id.get(processed.item_id)
            if matched_db_item is None or payload_data is None:
                logger.warning(
                    "job_batch_result_missing_item",
                    job_id=job_id,
                    item_id=processed.item_id,
                )
                continue

            item_persistence = None
            persist_error: Exception | None = None
            try:
                async with session_factory() as session:
                    item_persistence = await persist_processed_item_result(
                        session,
                        job_id=job_id,
                        item_id=matched_db_item.id,
                        file_path=matched_db_item.file_path,
                        processed=processed,
                        payload_data=payload_data,
                        executor=executor,
                        config=config,
                        job_context=job_context,
                        summary=summary,
                        configured_level=utility_log_level,
                        persist_log=persist_log,
                        completed_at=timestamp_factory(),
                    )
                    if item_persistence is None:
                        continue
                    await session.commit()
            except Exception as exc:
                persist_error = exc
                logger.error(
                    "job_item_persist_failed",
                    job_id=job_id,
                    item_id=matched_db_item.id,
                    error=str(exc),
                )

            if persist_error is not None:
                async with session_factory() as session:
                    failure_persistence = await persist_processed_item_failure(
                        session,
                        job_id=job_id,
                        item_id=matched_db_item.id,
                        file_path=matched_db_item.file_path,
                        processed=processed,
                        persist_error=persist_error,
                        summary=summary,
                        configured_level=utility_log_level,
                        persist_log=persist_log,
                        completed_at=timestamp_factory(),
                    )
                    if failure_persistence is None:
                        continue
                    await session.commit()

                summary.failed = failure_persistence.next_failed
                summary.warnings = failure_persistence.failure_warnings
                seen_item_ids.add(processed.item_id)
                continue

            if item_persistence is None:
                continue

            summary.completed += item_persistence.completed_delta
            summary.failed += item_persistence.failed_delta
            summary.skipped += item_persistence.skipped_delta
            summary.warnings += item_persistence.warning_delta
            seen_item_ids.add(processed.item_id)

            post_commit_logs = await executor.after_item_commit(
                payload_data,
                processed,
                config,
                job_context,
                summary,
                item_persistence.apply_result,
            )
            if post_commit_logs:
                async with session_factory() as session:
                    persist_post_commit_logs(
                        session,
                        runtime_logs=post_commit_logs,
                        persist_log=persist_log,
                        configured_level=utility_log_level,
                        job_id=job_id,
                        item_id=matched_db_item.id,
                        file_path=matched_db_item.file_path,
                        processed=processed,
                    )
                    await session.commit()
    except Exception as exc:
        logger.error(
            "job_batch_dispatch_failed",
            job_id=job_id,
            error=str(exc),
        )
        remaining_item_ids = remaining_batch_item_ids(
            batch_payloads,
            seen_item_ids,
        )

        for item_id in remaining_item_ids:
            processed = build_batch_dispatch_failure_item(item_id, exc)
            db_item = batch_items_by_id[item_id]

            async with session_factory() as session:
                persisted = await persist_batch_dispatch_failure_item(
                    session,
                    job_id=job_id,
                    item_id=db_item.id,
                    file_path=db_item.file_path,
                    processed=processed,
                    summary=summary,
                    configured_level=utility_log_level,
                    persist_log=persist_log,
                    completed_at=timestamp_factory(),
                )
                if not persisted:
                    continue
                await session.commit()


async def process_dispatch_batches(
    *,
    session_factory: Callable[[], Any],
    job_id: str,
    job_type: str,
    executor: JobExecutor,
    config: dict[str, Any],
    job_context: dict[str, Any] | None,
    summary: JobRunSummary,
    pending_items: list[UtilityJobItem],
    batch_size: int,
    worker_pool: Any,
    get_utility_log_level: Callable[[Any], Awaitable[str]],
    persist_log: Callable[..., None],
    logger: Any,
    timestamp_factory: Callable[[], str],
    project_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
) -> None:
    """Checkpoint, lease, and process pending utility items in batches."""
    for batch_start in range(0, len(pending_items), batch_size):
        async with session_factory() as session:
            checkpoint = await prepare_batch_checkpoint(
                session,
                job_id=job_id,
                summary=summary,
                get_utility_log_level=get_utility_log_level,
            )
            utility_log_level = checkpoint.utility_log_level
            if not checkpoint.should_continue:
                break

        async with session_factory() as session:
            batch_items = await lease_dispatch_batch(
                session,
                pending_items=pending_items,
                batch_start=batch_start,
                batch_size=batch_size,
                started_at=timestamp_factory(),
            )

        if project_progress is not None and batch_items:
            await project_progress(job_id, batch_items[0].id)

        await process_dispatch_batch(
            session_factory=session_factory,
            job_id=job_id,
            job_type=job_type,
            executor=executor,
            config=config,
            job_context=job_context,
            summary=summary,
            utility_log_level=utility_log_level,
            batch_items=batch_items,
            worker_pool=worker_pool,
            persist_log=persist_log,
            logger=logger,
            timestamp_factory=timestamp_factory,
        )
        if project_progress is not None:
            await project_progress(job_id, None)
