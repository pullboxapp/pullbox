"""Worker pool manager — execution-mode-aware batch dispatcher for utilities.

Dispatches work items through serial, thread, or process executors,
assigns worker IDs for UI tracking, collects results, and handles
worker crashes gracefully.
"""

from __future__ import annotations

import asyncio
import tempfile
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.core.exceptions import JobCancelledError
from pullbox.utilities.base_executor import (
    ExecutionMode,
    ItemResult,
    JobExecutor,
    ProcessedItem,
)
from pullbox.utilities.cancellation import check_cancelled, worker_cancellation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger(__name__)


def _execute_in_worker(
    executor: JobExecutor,
    item_data: dict[str, Any],
    job_config: dict[str, Any],
    job_context: dict[str, Any] | None,
    worker_id: int,
    cancellation_marker: str | None = None,
) -> ProcessedItem:
    """Run process_item in a worker process. Must be a module-level function for pickling."""
    try:
        with worker_cancellation(cancellation_marker):
            check_cancelled()
            result = executor.run_process_item(item_data, job_config, job_context)
        if result is None:
            return ProcessedItem(
                item_id=item_data.get("id", "unknown"),
                result=ItemResult.FAILED,
                error_message="process_item returned None instead of ProcessedItem",
                worker_id=worker_id,
            )
        result.worker_id = worker_id
        return result
    except JobCancelledError:
        return ProcessedItem(
            item_id=item_data.get("id", "unknown"),
            result=ItemResult.CANCELLED,
            worker_id=worker_id,
            log_entries=[("INFO", "Stopped before the next safe operation; source retained.", {})],
        )
    except Exception as exc:
        return ProcessedItem(
            item_id=item_data.get("id", "unknown"),
            result=ItemResult.FAILED,
            error_message=str(exc),
            worker_id=worker_id,
            log_entries=[("ERROR", f"Worker exception: {exc}", {})],
        )


class WorkerPool:
    """Execution-mode-aware wrapper for dispatching utility work item batches.

    Wraps ProcessPoolExecutor / ThreadPoolExecutor / serial execution with
    worker ID assignment, result collection, and graceful crash handling.
    """

    def __init__(
        self,
        *,
        execution_mode: ExecutionMode = ExecutionMode.PROCESS,
        max_workers: int = 4,
    ) -> None:
        self._execution_mode = execution_mode
        self._max_workers = max_workers
        self._pool: Executor | None
        if execution_mode == ExecutionMode.PROCESS:
            self._pool = ProcessPoolExecutor(max_workers=max_workers)
        elif execution_mode == ExecutionMode.THREAD:
            self._pool = ThreadPoolExecutor(max_workers=max_workers)
        elif execution_mode == ExecutionMode.SERIAL:
            self._pool = ThreadPoolExecutor(max_workers=1)
            self._max_workers = 1
        else:  # pragma: no cover - defensive fallback
            raise ValueError(f"Unsupported execution mode: {execution_mode}")
        self._shutdown = False
        # A private filesystem signal is picklable on spawn/fork and shared with threads.
        self._control_dir = tempfile.TemporaryDirectory(prefix="pullbox-utility-control-")
        self._cancellation_marker = Path(self._control_dir.name) / "cancel"

    def _ensure_active(self) -> None:
        """Raise if the pool is no longer available."""
        if self._shutdown or self._pool is None:
            raise RuntimeError("WorkerPool has been shut down")

    def _coerce_result(self, item_data: dict[str, Any], raw: object) -> ProcessedItem:
        """Normalize worker returns and exceptions into ProcessedItem results."""
        if isinstance(raw, Exception):
            return ProcessedItem(
                item_id=item_data.get("id", "unknown"),
                result=ItemResult.FAILED,
                error_message=f"Worker process failed: {raw}",
            )
        if isinstance(raw, ProcessedItem):
            return raw
        return ProcessedItem(
            item_id=item_data.get("id", "unknown"),
            result=ItemResult.FAILED,
            error_message=f"Unexpected result type: {type(raw).__name__}",
        )

    async def _run_batch_future(
        self,
        index: int,
        item_data: dict[str, Any],
        future: asyncio.Future[ProcessedItem],
    ) -> tuple[int, ProcessedItem]:
        """Await one submitted worker future and return its original batch index."""
        raw_result: object
        try:
            raw_result = await future
        except Exception as exc:  # pragma: no cover - exercised via caller contract
            raw_result = exc
        return index, self._coerce_result(item_data, raw_result)

    async def iter_batch_results(
        self,
        items: list[dict[str, Any]],
        executor: JobExecutor,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProcessedItem]:
        """Yield processed items as soon as each worker completes.

        This lets callers persist progress incrementally instead of waiting
        for the slowest item in the batch to finish.
        """
        self._ensure_active()

        if not items:
            return

        loop = asyncio.get_running_loop()
        pending: list[asyncio.Future[tuple[int, ProcessedItem]]] = []

        for idx, item_data in enumerate(items):
            worker_id = (idx % self._max_workers) + 1
            future = loop.run_in_executor(
                self._pool,
                _execute_in_worker,
                executor,
                item_data,
                job_config,
                job_context,
                worker_id,
                str(self._cancellation_marker),
            )
            pending.append(asyncio.create_task(self._run_batch_future(idx, item_data, future)))

        for completed in asyncio.as_completed(pending):
            _, result = await completed
            yield result

    async def process_batch(
        self,
        items: list[dict[str, Any]],
        executor: JobExecutor,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[ProcessedItem]:
        """Dispatch a batch of items to the worker pool and collect results.

        Args:
            items: List of item data dicts (from generate_items or DB).
            executor: The JobExecutor whose process_item will be called.
            job_config: The job's config dict passed to each process_item call.

        Returns:
            List of ProcessedItem results, one per input item.

        Raises:
            RuntimeError: If called after shutdown.
        """
        self._ensure_active()

        results_by_id: dict[str, ProcessedItem] = {}
        async for result in self.iter_batch_results(items, executor, job_config, job_context):
            results_by_id[result.item_id] = result

        results = [
            results_by_id.get(
                item["id"],
                ProcessedItem(
                    item_id=item["id"],
                    result=ItemResult.FAILED,
                    error_message="Worker did not return a result",
                ),
            )
            for item in items
        ]
        return results

    def shutdown(self) -> None:
        """Shut down the worker pool, waiting for in-progress items."""
        if self._pool is not None and not self._shutdown:
            self._pool.shutdown(wait=True)
            self._shutdown = True
            self._pool = None
            self._control_dir.cleanup()

    def request_cancel(self) -> None:
        """Ask workers to stop before the next safe operation."""
        if not self._shutdown:
            self._cancellation_marker.touch(exist_ok=True)
