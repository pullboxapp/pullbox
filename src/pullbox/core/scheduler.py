"""Pullbox task scheduler — APScheduler integration for background task execution.

Wraps APScheduler's AsyncIOScheduler with execution logging, in-memory
execution tracking, overlap prevention, and a decorator-based registration
API.  The scheduler starts and stops with the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.events import (  # type: ignore[import-untyped]
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy.exc import OperationalError

from pullbox.config import get_settings
from pullbox.core.log_deduper import log_deduped_warning
from pullbox.core.log_sanitizer import sanitize_log_string
from pullbox.core.scheduler_context import (
    get_current_task_run_id,
    get_current_task_trigger_type,
)
from pullbox.core.scheduler_error_helpers import (
    is_missing_task_stats_table_error,
    logical_task_id,
)
from pullbox.core.scheduler_manual_queue import (
    ManualTaskRequest,
    manual_queue_position,
    rebalance_manual_queue,
)
from pullbox.core.scheduler_persistence_policy import (
    coarse_persist_due,
    is_hot_task,
    should_persist_task_stat,
)
from pullbox.core.scheduler_registry import (
    RegisteredTask,
    get_registered_tasks,
    scheduled_task,
)
from pullbox.core.scheduler_registry import (
    task_registry as _task_registry,
)
from pullbox.core.scheduler_run_context import (
    bind_scheduler_run_context,
    reset_scheduler_run_context,
)
from pullbox.core.scheduler_stats import (
    TaskExecutionResult,
    TaskStats,
    merge_stats_into,
    resolve_interval_seconds,
)
from pullbox.core.scheduler_stats_persistence import (
    import_legacy_task_stats_sidecar,
    load_persisted_task_stat_rows,
    persist_task_stat_with_retries,
    resolve_legacy_task_stats_sidecar_path,
    resolve_runtime_db_url,
    upsert_task_stat_row,
)
from pullbox.core.scheduler_task_views import (
    build_job_summaries,
    build_scheduled_task_views,
)
from pullbox.core.sqlite_lock import is_sqlite_locked_error
from pullbox.services.import_activity import (
    has_active_import_scheduler_protection,
    is_missing_import_jobs_table_error,
)

_IMPORT_PROTECTION_ALLOWED_TASK_IDS = frozenset({"sync_story_arc_placements"})

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "PullboxScheduler",
    "RegisteredTask",
    "TaskExecutionResult",
    "TaskStats",
    "_task_registry",
    "get_current_task_run_id",
    "get_current_task_trigger_type",
    "get_registered_tasks",
    "get_scheduler",
    "scheduled_task",
]

logger = structlog.get_logger(__name__)
_MANUAL_QUEUE_DEFERRED_TASK_IDS = {"run_health_checks"}
_HOT_TASK_INTERVAL_SECONDS = 300
_HOT_TASK_PERSIST_WINDOW_SECONDS = 300.0


# ── Scheduler ─────────────────────────────────────────────────


class PullboxScheduler:
    """APScheduler wrapper with execution logging, tracking, and overlap prevention.

    Provides:
    - Automatic start / complete / error logging with duration tracking
    - Per-task stats tracking (last execution, duration, status)
    - ``max_instances=1`` to skip a run when the previous one is still active
    - ``coalesce=True`` to merge missed runs into a single execution
    - Listeners that log missed and skipped-due-to-overlap executions
    - Manual trigger support for running tasks on demand
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,  # 5-minute grace period
            },
        )
        self._started = False
        self._task_stats: dict[str, TaskStats] = {}
        self._display_names: dict[str, str] = {}
        self._running_counts: dict[str, int] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._registered_tasks: dict[str, RegisteredTask] = {}
        self._task_interval_seconds: dict[str, int | None] = {}
        self._task_last_persisted_at: dict[str, float] = {}
        self._task_last_persisted_status: dict[str, str | None] = {}
        self._exclusive_tasks: set[str] = set()
        self._exclusive_active_task_id: str | None = None
        self._execution_admission_lock = asyncio.Lock()
        self._manual_queue: deque[ManualTaskRequest] = deque()
        self._manual_queue_set: set[str] = set()
        self._manual_worker: asyncio.Task[None] | None = None
        self._manual_current_task_id: str | None = None
        self._persist_lock = asyncio.Lock()
        self._task_stats_persistence_disabled = False
        self._import_protection_check_disabled = False
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        self._scheduler.add_listener(self._on_job_max_instances, EVENT_JOB_MAX_INSTANCES)

    async def load_persisted_stats(self, session: Any | None = None) -> None:
        """Load persisted task execution stats from the DB and merge them."""
        from pullbox.database import get_session_factory

        legacy_sidecar_path = self._get_legacy_task_stats_sidecar_path()

        async def _load(active_session: Any) -> None:
            if legacy_sidecar_path is not None:
                await self._import_legacy_task_stats_sidecar(active_session, legacy_sidecar_path)
            try:
                loaded_stats = await load_persisted_task_stat_rows(
                    active_session,
                    known_task_ids=self._known_task_ids(),
                )
                for loaded_stat in loaded_stats:
                    self._merge_task_stats(loaded_stat.task_id, loaded_stat.stats)
                    self._mark_stats_persisted(
                        loaded_stat.task_id,
                        loaded_stat.stats,
                        persisted_at=loaded_stat.persisted_at,
                    )
            except OperationalError as exc:
                if not is_missing_task_stats_table_error(exc):
                    raise

        if session is not None:
            await _load(session)
            return

        factory = get_session_factory()
        async with factory() as own_session:
            await _load(own_session)

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler and begin executing registered jobs."""
        self._scheduler.start()
        self._started = True
        jobs = self._scheduler.get_jobs()
        logger.debug("scheduler_started", job_count=len(jobs))
        for job in jobs:
            logger.debug(
                "scheduler_job_registered",
                job_id=job.id,
                next_run=str(job.next_run_time),
            )

    def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler, optionally waiting for running jobs to finish."""
        if self._started:
            self._scheduler.shutdown(wait=wait)
            self._started = False
            if self._manual_worker is not None and not self._manual_worker.done():
                self._manual_worker.cancel()
            self._manual_worker = None
            self._manual_current_task_id = None
            self._manual_queue.clear()
            self._manual_queue_set.clear()
            self._exclusive_active_task_id = None
            logger.debug("scheduler_stopped")

    @property
    def running(self) -> bool:
        """Whether the scheduler is currently running."""
        return self._started

    # ── Job Management ─────────────────────────────────────────

    def add_job(
        self,
        func: Callable[..., Any],
        trigger: str,
        *,
        task_id: str,
        display_name: str = "",
        exclusive: bool = False,
        **trigger_kwargs: Any,
    ) -> None:
        """Add a single job wrapped with execution logging.

        Args:
            func: Async callable to execute on schedule.
            trigger: APScheduler trigger type (``"interval"`` or ``"cron"``).
            task_id: Unique identifier for this job.
            display_name: Human-readable name for the UI.
            **trigger_kwargs: Trigger-specific parameters.
        """
        wrapped = self._wrap_task(func, task_id, exclusive=exclusive)
        self._scheduler.add_job(
            wrapped,
            trigger,
            id=task_id,
            name=task_id,
            replace_existing=True,
            **trigger_kwargs,
        )
        if display_name:
            self._display_names[task_id] = display_name
        if exclusive:
            self._exclusive_tasks.add(task_id)
        if task_id not in self._task_stats:
            self._task_stats[task_id] = TaskStats()
        self._task_interval_seconds[task_id] = resolve_interval_seconds(trigger, trigger_kwargs)

    def register_tasks(
        self,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Bulk-register all ``@scheduled_task``-decorated tasks.

        Args:
            overrides: Optional mapping of ``task_id`` → trigger kwargs
                that override the decorator defaults.  This allows schedule
                configuration via application settings.
        """
        for task in _task_registry:
            kwargs = {**task.trigger_kwargs}
            if overrides and task.task_id in overrides:
                kwargs.update(overrides[task.task_id])
            self._registered_tasks[task.task_id] = task
            self.add_job(
                task.func,
                task.trigger,
                task_id=task.task_id,
                display_name=task.display_name,
                exclusive=task.exclusive,
                **kwargs,
            )

    def run_task_now(self, task_id: str) -> str | None:
        """Queue immediate execution of a registered task.

        Returns the queue status for the request, or ``None`` when the task id
        is unknown.
        """
        task = self._registered_tasks.get(task_id)
        if task is None:
            for registered in _task_registry:
                if registered.task_id == task_id:
                    task = registered
                    self._registered_tasks[task_id] = registered
                    break
        if task is None:
            return None

        if self._running_counts.get(task_id, 0) > 0 or self._manual_current_task_id == task_id:
            logger.info("task_manual_run_already_running", task_id=task_id)
            return "already_running"

        if task_id in self._manual_queue_set:
            logger.info("task_manual_run_already_queued", task_id=task_id)
            return "already_queued"

        request_id = structlog.contextvars.get_contextvars().get("request_id")
        trigger_request_id = request_id if isinstance(request_id, str) else None
        self._manual_queue.append(
            ManualTaskRequest(task_id=task_id, trigger_request_id=trigger_request_id)
        )
        self._manual_queue_set.add(task_id)
        self._rebalance_manual_queue()
        if task_id not in self._task_stats:
            self._task_stats[task_id] = TaskStats()
        self._ensure_manual_worker()
        logger.info(
            "task_triggered_manually",
            task_id=task_id,
            queue_position=self._manual_queue_position(task_id),
            trigger_request_id=trigger_request_id,
        )
        return "queued"

    def get_jobs(self) -> list[dict[str, str]]:
        """Return summary info for all scheduled jobs."""
        return build_job_summaries(self._visible_jobs())

    def get_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Return detailed info for all scheduled tasks for the UI."""
        return build_scheduled_task_views(
            self._visible_jobs(),
            task_stats=self._task_stats,
            display_names=self._display_names,
            running_counts=self._running_counts,
            queued_task_ids=self._manual_queue_set,
            manual_queue_position=self._manual_queue_position,
        )

    def schedule_task_continuation(
        self,
        task_id: str,
        *,
        run_at: datetime,
        interval_seconds: int = 3600,
    ) -> bool:
        """Schedule a hidden recurring continuation for a durable logical task."""
        task = self._registered_tasks.get(task_id)
        if task is None:
            task = next((item for item in _task_registry if item.task_id == task_id), None)
        if task is None:
            return False
        job_id = self._continuation_job_id(task_id)
        wrapped = self._wrap_task(
            task.func,
            task_id,
            trigger_type="scheduled",
            exclusive=task.exclusive,
        )
        self._scheduler.add_job(
            wrapped,
            "interval",
            id=job_id,
            name=job_id,
            replace_existing=True,
            seconds=max(1, interval_seconds),
            next_run_time=run_at,
        )
        return True

    def clear_task_continuation(self, task_id: str) -> None:
        """Remove a hidden continuation if it is currently registered."""
        job_id = self._continuation_job_id(task_id)
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)

    def delay_task_next_run(self, task_id: str, *, run_at: datetime) -> bool:
        """Move a recurring task's next execution without changing its interval."""
        if self._scheduler.get_job(task_id) is None:
            return False
        self._scheduler.modify_job(task_id, next_run_time=run_at)
        return True

    def _visible_jobs(self) -> list[Any]:
        """Return user-facing jobs without internal continuation plumbing."""
        return [job for job in self._scheduler.get_jobs() if not job.id.endswith("__continuation")]

    @staticmethod
    def _continuation_job_id(task_id: str) -> str:
        return f"{task_id}__continuation"

    # ── Private ────────────────────────────────────────────────

    def _wrap_task(
        self,
        func: Callable[..., Any],
        task_id: str,
        trigger_type: str = "scheduled",
        *,
        exclusive: bool = False,
        trigger_request_id: str | None = None,
    ) -> Callable[..., Any]:
        """Wrap an async task function with start / complete / error logging and tracking."""
        scheduler = self

        @wraps(func)
        async def wrapper() -> None:
            log = logger.bind(task_id=task_id)
            reserved_exclusive = False
            if trigger_type in {"scheduled", "manual"} and await scheduler._defer_task_for_import(
                task_id,
                log,
                trigger_type=trigger_type,
            ):
                return

            async with scheduler._execution_admission_lock:
                active_exclusive = scheduler._exclusive_active_task_id
                if active_exclusive is not None and active_exclusive != task_id:
                    stats = scheduler._task_stats.setdefault(task_id, TaskStats())
                    stats.last_exclusive_block_at = datetime.now(UTC).isoformat()
                    stats.exclusive_block_count += 1
                    await scheduler._persist_task_stat(
                        task_id,
                        stats,
                        trigger_type=trigger_type,
                        reason="exclusive_block",
                    )
                    log.debug(
                        "task_skipped_exclusive",
                        trigger_type=trigger_type,
                        exclusive_task_id=active_exclusive,
                    )
                    return

                if scheduler._running_counts.get(task_id, 0) > 0:
                    stats = scheduler._task_stats.setdefault(task_id, TaskStats())
                    stats.last_overlap_at = datetime.now(UTC).isoformat()
                    stats.overlap_count += 1
                    await scheduler._persist_task_stat(
                        task_id,
                        stats,
                        trigger_type=trigger_type,
                        reason="overlap",
                    )
                    scheduler._log_task_overlap(task_id, trigger_type=trigger_type)
                    return

                if exclusive:
                    scheduler._exclusive_active_task_id = task_id
                    reserved_exclusive = True

            if exclusive:
                while any(
                    running_task_id != task_id and count > 0
                    for running_task_id, count in scheduler._running_counts.items()
                ):
                    await asyncio.sleep(0.05)

            log.debug("task_started", trigger_type=trigger_type)
            start = time.monotonic()
            scheduler._running_counts[task_id] = scheduler._running_counts.get(task_id, 0) + 1
            run_context = bind_scheduler_run_context(
                task_id=task_id,
                trigger_type=trigger_type,
                trigger_request_id=trigger_request_id,
            )
            started_dt = datetime.now(UTC)
            started = started_dt.isoformat()

            try:
                stats = scheduler._task_stats.setdefault(task_id, TaskStats())
                stats.running_since = started
                result = await func()
                elapsed = time.monotonic() - start
                ended_dt = datetime.now(UTC)
                ended = ended_dt.isoformat()

                stats.last_execution = ended
                stats.last_duration_seconds = round(elapsed, 2)
                completed_status = (
                    result.status if isinstance(result, TaskExecutionResult) else "completed"
                )
                stats.last_status = completed_status
                stats.running_since = None
                await scheduler._persist_task_stat(
                    task_id,
                    stats,
                    trigger_type=trigger_type,
                    reason=completed_status,
                )

                log.debug(
                    "task_completed",
                    duration_seconds=round(elapsed, 2),
                    logical_status=completed_status,
                )
            except asyncio.CancelledError:
                elapsed = time.monotonic() - start
                ended_dt = datetime.now(UTC)
                ended = ended_dt.isoformat()

                stats = scheduler._task_stats.setdefault(task_id, TaskStats())
                stats.last_execution = ended
                stats.last_duration_seconds = round(elapsed, 2)
                stats.last_status = "cancelled"
                stats.running_since = None
                await scheduler._persist_task_stat(
                    task_id,
                    stats,
                    trigger_type=trigger_type,
                    reason="cancelled",
                )

                log.warning("task_cancelled", duration_seconds=round(elapsed, 2))
                raise
            except Exception as exc:
                elapsed = time.monotonic() - start
                ended_dt = datetime.now(UTC)
                ended = ended_dt.isoformat()
                error_message = sanitize_log_string(
                    f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
                )

                stats = scheduler._task_stats.setdefault(task_id, TaskStats())
                stats.last_execution = ended
                stats.last_duration_seconds = round(elapsed, 2)
                stats.last_status = "failed"
                stats.running_since = None
                await scheduler._persist_task_stat(
                    task_id,
                    stats,
                    trigger_type=trigger_type,
                    reason="failed",
                )

                log.exception(
                    "task_failed",
                    duration_seconds=round(elapsed, 2),
                    error_message=error_message,
                )
            finally:
                reset_scheduler_run_context(run_context)
                current = scheduler._running_counts.get(task_id, 0)
                if current <= 1:
                    scheduler._running_counts.pop(task_id, None)
                else:
                    scheduler._running_counts[task_id] = current - 1
                if reserved_exclusive:
                    async with scheduler._execution_admission_lock:
                        if scheduler._exclusive_active_task_id == task_id:
                            scheduler._exclusive_active_task_id = None

        return wrapper

    async def _defer_task_for_import(self, task_id: str, log: Any, *, trigger_type: str) -> bool:
        """Skip scheduler-managed background work while an import owns the runtime."""
        from pullbox.database import database_maintenance_reason

        maintenance_reason = database_maintenance_reason()
        if maintenance_reason:
            stats = self._task_stats.setdefault(task_id, TaskStats())
            ended = datetime.now(UTC).isoformat()
            stats.last_execution = ended
            stats.last_duration_seconds = 0.0
            stats.last_status = "deferred"
            stats.running_since = None
            await self._persist_task_stat(
                task_id,
                stats,
                trigger_type=trigger_type,
                reason="database_maintenance",
            )
            log.info(
                "task_deferred_database_maintenance",
                trigger_type=trigger_type,
                maintenance_reason=maintenance_reason,
            )
            return True

        if task_id in _IMPORT_PROTECTION_ALLOWED_TASK_IDS:
            return False

        if self._import_protection_check_disabled:
            return False

        try:
            should_defer = await has_active_import_scheduler_protection()
        except Exception as exc:
            if is_missing_import_jobs_table_error(exc):
                self._import_protection_check_disabled = True
                log.debug("task_import_protection_check_disabled_missing_table")
                return False
            if not is_sqlite_locked_error(exc):
                log.warning("task_import_protection_check_failed", exc_info=True)
                return False
            should_defer = True

        if not should_defer:
            return False

        stats = self._task_stats.setdefault(task_id, TaskStats())
        ended = datetime.now(UTC).isoformat()
        stats.last_execution = ended
        stats.last_duration_seconds = 0.0
        stats.last_status = "deferred"
        stats.running_since = None
        await self._persist_task_stat(
            task_id,
            stats,
            trigger_type=trigger_type,
            reason="deferred",
        )
        log.info("task_deferred_active_import", trigger_type=trigger_type)
        return True

    def _ensure_manual_worker(self) -> None:
        """Start the manual queue worker if it is not already draining requests."""
        if self._manual_worker is not None and not self._manual_worker.done():
            return
        loop = asyncio.get_running_loop()
        self._manual_worker = loop.create_task(self._drain_manual_queue())
        self._background_tasks.add(self._manual_worker)
        self._manual_worker.add_done_callback(self._background_tasks.discard)

    async def _drain_manual_queue(self) -> None:
        """Execute queued manual task requests sequentially."""
        try:
            while self._manual_queue:
                queued = self._manual_queue.popleft()
                self._manual_queue_set.discard(queued.task_id)
                task = self._registered_tasks.get(queued.task_id)
                if task is None:
                    continue
                self._manual_current_task_id = queued.task_id
                wrapped = self._wrap_task(
                    task.func,
                    queued.task_id,
                    trigger_type="manual",
                    exclusive=task.exclusive,
                    trigger_request_id=queued.trigger_request_id,
                )
                try:
                    await wrapped()
                finally:
                    self._manual_current_task_id = None
        finally:
            self._manual_worker = None
            self._manual_current_task_id = None

    def _manual_queue_position(self, task_id: str) -> int | None:
        """Return a task's 1-based queue position, if it is queued."""
        return manual_queue_position(self._manual_queue, task_id)

    def _rebalance_manual_queue(self) -> None:
        """Keep deferred manual tasks at the back of the queue."""
        if len(self._manual_queue) < 2:
            return
        self._manual_queue = rebalance_manual_queue(
            self._manual_queue,
            deferred_task_ids=_MANUAL_QUEUE_DEFERRED_TASK_IDS,
        )

    async def _persist_task_stat(
        self,
        task_id: str,
        stats: TaskStats,
        *,
        trigger_type: str = "scheduled",
        reason: str = "completed",
    ) -> None:
        """Persist the latest execution stats per task for cross-request stability."""
        if self._task_stats_persistence_disabled:
            return
        if not self._should_persist_task_stat(
            task_id,
            stats,
            trigger_type=trigger_type,
            reason=reason,
        ):
            return
        await self._persist_task_stat_via_session(task_id, stats)

    async def _persist_task_stat_via_session(self, task_id: str, stats: TaskStats) -> None:
        """Persist task stats through the async session path."""
        from pullbox.database import get_session_factory

        factory = get_session_factory()
        await persist_task_stat_with_retries(
            session_factory=factory,
            persist_lock=self._persist_lock,
            task_id=task_id,
            stats=stats,
            upsert_task_stat_row=self._upsert_task_stat_row,
            mark_stats_persisted=self._mark_stats_persisted,
            log_missing_persisted_table=self._log_missing_persisted_table,
            disable_task_stats_persistence=self._disable_task_stats_persistence,
            is_hot_task=self._is_hot_task,
            log_persist_skipped_locked=self._log_persist_skipped_locked,
            log_persist_failed=self._log_persist_failed,
            sleep=asyncio.sleep,
        )

    async def _upsert_task_stat_row(self, session: Any, task_id: str, stats: TaskStats) -> None:
        """Upsert a scheduled task stats row into the main DB."""
        await upsert_task_stat_row(session, task_id, stats)

    def _runtime_db_url(self) -> str:
        """Return the active DB URL used by the scheduler persistence path."""
        from pullbox.database import get_session_factory

        return resolve_runtime_db_url(get_settings().db_url, get_session_factory())

    def _log_missing_persisted_table(
        self,
        *,
        task_id: str,
        table_name: str,
        phase: str,
    ) -> None:
        """Emit one actionable warning when a persisted scheduler table is unavailable."""
        db_url = self._runtime_db_url()
        log_deduped_warning(
            logger,
            "scheduler_persisted_table_missing",
            key=("scheduler_persisted_table_missing", table_name, db_url, phase),
            window_seconds=300.0,
            task_id=task_id,
            table_name=table_name,
            db_url=db_url,
            phase=phase,
            action_required=(
                "Apply the latest migrations and restart the app if this table was just added."
            ),
        )

    @staticmethod
    def _log_persist_skipped_locked(task_id: str) -> None:
        logger.debug(
            "scheduler_task_stats_persist_skipped_locked",
            task_id=task_id,
        )

    @staticmethod
    def _log_persist_failed(task_id: str) -> None:
        logger.warning(
            "scheduler_task_stats_persist_failed",
            task_id=task_id,
            exc_info=True,
        )

    def _disable_task_stats_persistence(self, *, task_id: str, exc: BaseException) -> None:
        """Fail dark when the scheduler stats store becomes unusable."""
        self._task_stats_persistence_disabled = True
        log_deduped_warning(
            logger,
            "scheduler_task_stats_persistence_disabled",
            key=("scheduler_task_stats_persistence_disabled", self._runtime_db_url()),
            window_seconds=300.0,
            task_id=task_id,
            db_url=self._runtime_db_url(),
            error=sanitize_log_string(str(exc)),
            action_required=(
                "Repair the SQLite database or recreate scheduled_task_stats. "
                "Task execution continues, but persisted task stats are disabled until restart."
            ),
        )

    @staticmethod
    def _get_legacy_task_stats_sidecar_path() -> Path | None:
        """Return the retired JSON sidecar path for one-time import."""
        from pullbox.database import get_session_factory

        db_url = resolve_runtime_db_url(get_settings().db_url, get_session_factory())
        return resolve_legacy_task_stats_sidecar_path(db_url)

    def _known_task_ids(self) -> set[str]:
        """Return known task IDs from the registry, registered jobs, and display metadata."""
        known = {task.task_id for task in _task_registry}
        known.update(self._registered_tasks)
        known.update(self._display_names)
        known.update(job.id for job in self._scheduler.get_jobs())
        return known

    async def _import_legacy_task_stats_sidecar(self, session: Any, sidecar_path: Path) -> None:
        """Import a legacy JSON sidecar into the DB once, then delete it."""
        await import_legacy_task_stats_sidecar(
            session,
            sidecar_path,
            known_task_ids=self._known_task_ids(),
            upsert_task_stat_row=self._upsert_task_stat_row,
            log_missing_persisted_table=self._log_missing_persisted_table,
        )

    def _merge_task_stats(self, task_id: str, persisted: TaskStats) -> None:
        """Merge persisted stats into the in-memory tracker, preferring newer executions."""
        current = self._task_stats.get(task_id)
        if current is None:
            self._task_stats[task_id] = persisted
            return

        merge_stats_into(current, persisted)

    def _mark_stats_persisted(
        self,
        task_id: str,
        stats: TaskStats,
        *,
        persisted_at: float | None = None,
    ) -> None:
        """Record when and how a task's stats were last persisted."""
        if persisted_at is None:
            persisted_at = time.time()
        self._task_last_persisted_at[task_id] = persisted_at
        self._task_last_persisted_status[task_id] = stats.last_status

    def _is_hot_task(self, task_id: str) -> bool:
        """Return True when a task's effective interval is under five minutes."""
        return is_hot_task(
            task_id,
            self._task_interval_seconds,
            hot_task_interval_seconds=_HOT_TASK_INTERVAL_SECONDS,
        )

    def _log_task_overlap(self, task_id: str, *, trigger_type: str | None = None) -> None:
        """Log overlap skips at debug for hot pollers and warning for normal tasks."""
        kwargs: dict[str, object] = {"task_id": task_id}
        if trigger_type is not None:
            kwargs["trigger_type"] = trigger_type
        log_method = logger.debug if self._is_hot_task(task_id) else logger.warning
        log_method("task_skipped_overlap", **kwargs)

    def _coarse_persist_due(self, task_id: str) -> bool:
        """Return True when a hot task's coarse persistence window has elapsed."""
        return coarse_persist_due(
            task_id,
            self._task_last_persisted_at,
            now=time.time(),
            hot_task_persist_window_seconds=_HOT_TASK_PERSIST_WINDOW_SECONDS,
        )

    def _should_persist_task_stat(
        self,
        task_id: str,
        stats: TaskStats,
        *,
        trigger_type: str,
        reason: str,
    ) -> bool:
        """Decide whether a task-stat update should hit the DB now."""
        return should_persist_task_stat(
            task_id,
            stats,
            trigger_type=trigger_type,
            reason=reason,
            interval_seconds_by_task=self._task_interval_seconds,
            last_persisted_at=self._task_last_persisted_at,
            last_persisted_status=self._task_last_persisted_status,
            now=time.time(),
            hot_task_interval_seconds=_HOT_TASK_INTERVAL_SECONDS,
            hot_task_persist_window_seconds=_HOT_TASK_PERSIST_WINDOW_SECONDS,
        )

    def _schedule_stat_persist(self, task_id: str, *, reason: str) -> None:
        """Persist a task's stats from synchronous scheduler callbacks when possible."""
        stats = self._task_stats.get(task_id)
        if stats is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("scheduler_task_stats_persist_skipped", task_id=task_id)
            return
        task = loop.create_task(
            self._persist_task_stat(
                task_id,
                stats,
                trigger_type="scheduled",
                reason=reason,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _on_job_missed(self, event: Any) -> None:
        """Log when a scheduled job execution was missed."""
        task_id = logical_task_id(event.job_id)
        stats = self._task_stats.setdefault(task_id, TaskStats())
        stats.last_missed_at = datetime.now(UTC).isoformat()
        stats.missed_count += 1
        self._schedule_stat_persist(task_id, reason="missed")
        logger.warning("task_missed", task_id=task_id)

    def _on_job_max_instances(self, event: Any) -> None:
        """Log when a job is skipped because a previous run is still active."""
        task_id = logical_task_id(event.job_id)
        stats = self._task_stats.setdefault(task_id, TaskStats())
        stats.last_overlap_at = datetime.now(UTC).isoformat()
        stats.overlap_count += 1
        self._schedule_stat_persist(task_id, reason="overlap")
        self._log_task_overlap(task_id)


# ── Module-level Singleton ────────────────────────────────────

_scheduler_instance: PullboxScheduler | None = None


def get_scheduler() -> PullboxScheduler:
    """Return the application scheduler singleton.

    Creates the ``PullboxScheduler`` on first call.  Subsequent calls
    return the same instance.
    """
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = PullboxScheduler()
    return _scheduler_instance
