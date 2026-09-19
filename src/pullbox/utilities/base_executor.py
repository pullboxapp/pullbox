"""Base executor interface for all utility features.

Defines the contract that all utility executors implement:
- generate_items: discover work items from config
- process_item: do the work (runs in worker process, must be picklable)
- rollback_item: undo completed work
- validate_config: pre-flight config validation
"""

from __future__ import annotations

import enum
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ExecutionMode(enum.StrEnum):
    """Execution strategy for utility item processing."""

    SERIAL = "serial"
    THREAD = "thread"
    PROCESS = "process"


class ItemResult(enum.StrEnum):
    """Outcome of processing a single work item."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class ProcessedItem:
    """Result of processing one work item in a worker process.

    MUST be fully picklable — no DB connections, file handles, loggers,
    or asyncio objects. Worker processes return these via IPC.

    The ``log_entries`` field collects log messages during processing.
    The queue manager bulk-inserts them into the DB after each batch.
    Each entry is ``(level, message, extra_dict)``.
    """

    item_id: str
    result: ItemResult
    before_state: dict[str, Any] = field(default_factory=dict)
    after_state: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error_message: str | None = None
    warning_message: str | None = None
    worker_id: int | None = None
    log_entries: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class RuntimeLogEntry:
    """Structured queue-side log entry emitted outside worker processes."""

    level: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)
    file_path: str | None = None


@dataclass
class ApplyResult:
    """Queue-side outcome from applying a processed item."""

    extra_logs: list[RuntimeLogEntry] = field(default_factory=list)
    post_commit_payload: dict[str, Any] = field(default_factory=dict)
    warning_increment: int = 0
    warning_message: str | None = None


@dataclass
class FinalizeResult:
    """Executor-specific job finalization output."""

    extra_logs: list[RuntimeLogEntry] = field(default_factory=list)
    final_parts: list[str] = field(default_factory=list)
    final_log_level: str | None = None
    error_message: str | None = None


@dataclass
class JobRunSummary:
    """Mutable per-job summary shared between queue and executor hooks."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    warnings: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class JobExecutor(ABC):
    """Abstract base for all utility executors.

    Subclasses implement the three abstract methods below. The
    JobQueueManager calls them during job lifecycle:

    1. validate_config() — before job is queued (fail fast on bad config)
    2. generate_items() — when job starts running (discover work items)
    3. process_item()   — per item, in worker process via ProcessPoolExecutor
    4. rollback_item()  — per item, when rollback is requested
    """

    execution_mode: ExecutionMode = ExecutionMode.PROCESS

    @abstractmethod
    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover and return work items for this job.

        Each returned dict becomes one ``utility_job_items`` row. Must contain
        at minimum: ``{"file_path": str, "operation": str}``. Additional fields
        are executor-specific and stored in the item's ``before_state``.

        Called once when the job transitions from QUEUED to RUNNING.
        For resumed jobs, the queue manager loads remaining PENDING items
        from the DB instead of calling this again.
        """
        ...

    @abstractmethod
    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Process a single work item. Runs in a worker process.

        CRITICAL: This method runs in a ProcessPoolExecutor. It MUST:
        - Be synchronous (no async/await)
        - Use only picklable arguments and return values
        - Not access the database, event bus, or FastAPI app state
        - Collect log entries in ProcessedItem.log_entries (not structlog)
        - Handle its own exceptions and return FAILED, never raise

        Args:
            item_data: The dict returned by generate_items() for this item.
            job_config: The job's config dict (from utility_jobs.config).

        Returns:
            ProcessedItem with result, state snapshots, and log entries.
        """
        ...

    @abstractmethod
    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Undo a completed item. Same constraints as process_item().

        Uses ``after_state`` from the original ProcessedItem to know what
        to reverse. Returns a new ProcessedItem with the rollback result.
        """
        ...

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        """Validate job config before queuing. Return list of error messages.

        Empty list = config is valid. Non-empty = job rejected with errors.
        Override in subclasses to add executor-specific validation.
        """
        return []

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Build shared context once per job before any items are generated."""
        return {}

    async def apply_item_result(
        self,
        session: Any,
        item: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
    ) -> ApplyResult:
        """Apply queue-side side effects for a processed item."""
        return ApplyResult()

    async def after_item_commit(
        self,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
        apply_result: ApplyResult,
    ) -> list[RuntimeLogEntry]:
        """Run any non-blocking follow-up work after the item commit succeeds."""
        return []

    async def finalize_job(
        self,
        session: Any,
        job: Any,
        summary: JobRunSummary,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
    ) -> FinalizeResult:
        """Allow executors to add final logs or adjust terminal job metadata."""
        return FinalizeResult()

    def get_execution_mode(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ExecutionMode:
        """Return the execution mode for this utility job."""
        return self.execution_mode

    async def run_generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for generate_items during hook migration."""
        if _accepts_context(self.generate_items, expected_without_context=1):
            return await self.generate_items(job_config, job_context)
        return await self.generate_items(job_config)

    def run_process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Compatibility wrapper for process_item during hook migration."""
        if _accepts_context(self.process_item, expected_without_context=2):
            return self.process_item(item_data, job_config, job_context)
        return self.process_item(item_data, job_config)

    def run_rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Compatibility wrapper for rollback_item during hook migration."""
        if _accepts_context(self.rollback_item, expected_without_context=2):
            return self.rollback_item(item_data, job_config, job_context)
        return self.rollback_item(item_data, job_config)


def _accepts_context(method: Any, *, expected_without_context: int) -> bool:
    """Return True when a bound executor method accepts the new context arg."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return True
    return len(params) > expected_without_context
