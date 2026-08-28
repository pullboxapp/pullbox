"""Job queue manager — orchestrates utility job lifecycle.

Central singleton that manages job creation, state transitions,
startup recovery, batch dispatch, checkpointing, and job controls
(pause/resume/cancel/rollback). Runs one job at a time (serial queue).

State machine follows the DownloadState pattern from models/download.py
for cross-sprint consistency.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from pullbox.services.utility_operation_progress import project_utility_operation_progress
from pullbox.utilities.job_queue_batch_processing import process_dispatch_batches
from pullbox.utilities.job_queue_config import (
    get_utility_log_level,
    get_utility_worker_count,
)
from pullbox.utilities.job_queue_creation import build_queued_job, build_rollback_job
from pullbox.utilities.job_queue_dispatch_start import (
    start_next_dispatch_job,
)
from pullbox.utilities.job_queue_finalization import (
    finalize_dispatch_job,
)
from pullbox.utilities.job_queue_item_preparation import (
    mark_item_generation_failed,
    prepare_dispatch_items,
)
from pullbox.utilities.job_queue_ordering import (
    build_resume_queue_order,
    next_queue_position,
    queued_jobs_in_order,
    resequence_queued_jobs,
)
from pullbox.utilities.job_queue_runtime_state import (
    load_dispatch_runtime_state,
)
from pullbox.utilities.job_queue_state import (
    CANCELLABLE_STATES as _CANCELLABLE_STATES,
)
from pullbox.utilities.job_queue_state import (
    ROLLBACKABLE_STATES as _ROLLBACKABLE_STATES,
)
from pullbox.utilities.job_queue_state import (
    VALID_TRANSITIONS,
    transition_job_state,
)
from pullbox.utilities.job_queue_worker_runtime import build_worker_runtime
from pullbox.utilities.logging_config import persist_runtime_utility_log
from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    UtilityJob,
    UtilityJobItem,
)
from pullbox.utilities.worker_pool import WorkerPool

logger = structlog.get_logger(__name__)
__all__ = ["VALID_TRANSITIONS", "JobQueueManager"]

if TYPE_CHECKING:
    from pullbox.utilities.base_executor import JobExecutor


# ── JobQueueManager ───────────────────────────────────────────


class JobQueueManager:
    """Orchestrates utility job lifecycle with serial execution.

    Manages job creation, state machine enforcement, startup recovery,
    batch dispatch via WorkerPool, checkpointing, and job controls
    (pause, resume, cancel, rollback).
    """

    def __init__(
        self,
        session_factory: Any,
    ) -> None:
        self._session_factory = session_factory
        self._registry: dict[str, type[JobExecutor]] = {}
        self._dispatch_lock = asyncio.Lock()

    # ── Executor Registry ──────────────────────────────────────

    def register_executor(self, job_type: str, executor_class: type[JobExecutor]) -> None:
        """Register an executor class for a job type."""
        self._registry[job_type] = executor_class

    def get_executor(self, job_type: str) -> JobExecutor | None:
        """Look up and instantiate an executor for the given job type."""
        cls = self._registry.get(job_type)
        if cls is None:
            return None
        if job_type == JobType.ROLLBACK:
            return cls(executor_registry=self._registry)  # type: ignore[call-arg]
        return cls()

    async def _project_dispatch_progress(
        self,
        job_id: str,
        current_item_id: str | None = None,
    ) -> None:
        """Publish one utility snapshot from durable queue state."""
        async with self._session_factory() as session:
            job = await session.get(UtilityJob, job_id)
            if job is None:
                return
            current_item = (
                await session.get(UtilityJobItem, current_item_id)
                if current_item_id is not None
                else None
            )
            await project_utility_operation_progress(
                session,
                job,
                current_item=current_item,
            )
            await session.commit()

    async def _create_rollback_job(
        self,
        session: AsyncSession,
        job: UtilityJob,
        *,
        created_by: str | None = None,
    ) -> UtilityJob:
        """Create a queued rollback child job for ``job``."""
        existing_result = await session.execute(
            select(UtilityJob)
            .where(
                UtilityJob.parent_job_id == job.id,
                UtilityJob.job_type == JobType.ROLLBACK,
            )
            .order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc())
            .limit(1)
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None and existing.state not in {JobState.FAILED, JobState.CANCELLED}:
            raise ValueError(
                "Rollback already queued or completed for this job. "
                "Delete the existing rollback entry before retrying."
            )

        rollback_job = build_rollback_job(
            job,
            queue_position=await next_queue_position(session),
            created_by=created_by,
        )
        session.add(rollback_job)
        await session.flush()
        await project_utility_operation_progress(session, rollback_job)
        logger.info(
            "rollback_job_created",
            rollback_job_id=rollback_job.id,
            parent_job_id=job.id,
        )
        return rollback_job

    @staticmethod
    def _persist_utility_log(
        session: AsyncSession,
        *,
        configured_level: str,
        job_id: str,
        level: str,
        message: str,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
        worker_id: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Persist one utility log entry and mirror it to utilities.log."""
        persist_runtime_utility_log(
            session,
            configured_level=configured_level,
            job_id=job_id,
            item_id=item_id,
            level=level,
            message=message,
            file_path=file_path,
            extra=extra,
            worker_id=worker_id,
            duration_ms=duration_ms,
        )

    # ── State Machine ──────────────────────────────────────────

    def transition(self, job: UtilityJob, new_state: JobState) -> None:
        """Enforce valid state transitions and update timestamps.

        Raises:
            ValueError: If the transition is not allowed.
        """
        old = transition_job_state(job, new_state)
        logger.debug(
            "job_state_transition",
            job_id=job.id,
            old=old.value,
            new=new_state.value,
        )

    # ── Job Creation ───────────────────────────────────────────

    async def create_job(
        self,
        session: AsyncSession,
        job_type: str,
        display_name: str,
        config: dict[str, Any],
        created_by: str | None = None,
    ) -> UtilityJob:
        """Create a new job and add it to the queue.

        Args:
            session: Active database session.
            job_type: One of the JobType enum values.
            display_name: Human-readable name for the UI.
            config: Job-specific configuration dict.
            created_by: Username who initiated the job.

        Returns:
            The created UtilityJob instance.
        """
        next_pos = await next_queue_position(session)

        job = build_queued_job(
            job_type=job_type,
            display_name=display_name,
            config=config,
            queue_position=next_pos,
            created_by=created_by,
        )
        session.add(job)
        await session.flush()
        await project_utility_operation_progress(session, job)

        logger.info(
            "job_created",
            job_id=job.id,
            job_type=job_type,
            queue_position=next_pos,
        )
        return job

    # ── Dispatch ──────────────────────────────────────────────

    async def dispatch_next(self) -> None:
        """Drain queued jobs until the serial queue becomes idle.

        Finds the next QUEUED job, transitions it to RUNNING,
        processes it to a terminal or paused state, then immediately
        advances to the next QUEUED job if the execution slot is free.
        Runs one job at a time (serial queue).
        """
        async with self._dispatch_lock:
            while True:
                async with self._session_factory() as session:
                    start_result = await start_next_dispatch_job(
                        session,
                        get_executor=self.get_executor,
                        get_utility_log_level=get_utility_log_level,
                        persist_log=self._persist_utility_log,
                        timestamp_factory=lambda: datetime.now(UTC).isoformat(),
                    )
                    if start_result.status == "idle":
                        return
                    if start_result.log_event is not None:
                        logger.info(start_result.log_event, **start_result.log_context)
                    started_job = start_result.started_job
                    if started_job is None:
                        continue
                    current_job = await session.get(UtilityJob, started_job.job_id)
                    if current_job is not None:
                        await project_utility_operation_progress(session, current_job)
                        await session.commit()

                job_id = started_job.job_id
                job_type = started_job.job_type
                executor = started_job.executor
                raw_config = started_job.raw_config
                config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
                pending_items: list[UtilityJobItem]
                job_context: dict[str, Any] | None = None

                async with self._session_factory() as session:
                    runtime = await load_dispatch_runtime_state(
                        session,
                        job_id=job_id,
                        job_type=job_type,
                        get_worker_count=get_utility_worker_count,
                        get_utility_log_level=get_utility_log_level,
                    )
                summary = runtime.summary
                worker_count = runtime.worker_count

                try:
                    prepared_items = await prepare_dispatch_items(
                        self._session_factory,
                        job_id=job_id,
                        executor=executor,
                        config=config,
                    )
                    if prepared_items is None:
                        continue
                    job_context = prepared_items.job_context
                    pending_items = prepared_items.pending_items
                except Exception as exc:
                    async with self._session_factory() as session:
                        await mark_item_generation_failed(
                            session,
                            job_id=job_id,
                            exc=exc,
                        )
                    logger.error("job_item_generation_failed", job_id=job_id, error=str(exc))
                    continue

                worker_runtime = build_worker_runtime(
                    executor=executor,
                    config=config,
                    job_context=job_context,
                    worker_count=worker_count,
                    worker_pool_factory=WorkerPool,
                )
                batch_size = worker_runtime.batch_size
                worker_pool = worker_runtime.worker_pool
                try:
                    await process_dispatch_batches(
                        session_factory=self._session_factory,
                        job_id=job_id,
                        job_type=job_type,
                        executor=executor,
                        config=config,
                        job_context=job_context,
                        summary=summary,
                        pending_items=pending_items,
                        batch_size=batch_size,
                        worker_pool=worker_pool,
                        get_utility_log_level=get_utility_log_level,
                        persist_log=self._persist_utility_log,
                        project_progress=self._project_dispatch_progress,
                        logger=logger,
                        timestamp_factory=lambda: datetime.now(UTC).isoformat(),
                    )
                finally:
                    worker_pool.shutdown()

                async with self._session_factory() as session:
                    finalization = await finalize_dispatch_job(
                        session,
                        job_id=job_id,
                        job_type=job_type,
                        executor=executor,
                        summary=summary,
                        config=config,
                        job_context=job_context,
                        get_utility_log_level=get_utility_log_level,
                        persist_log=self._persist_utility_log,
                        transition_job=self.transition,
                    )
                    if finalization.log_event is not None:
                        logger.info(finalization.log_event, **finalization.log_context)
                    finalized_job = await session.get(UtilityJob, job_id)
                    if finalized_job is not None:
                        await project_utility_operation_progress(session, finalized_job)
                        await session.commit()

    # ── Startup Recovery ───────────────────────────────────────

    async def recover_interrupted_jobs(self, session: AsyncSession) -> int:
        """Recover jobs interrupted by a server crash.

        - RUNNING/PAUSING jobs → PAUSED
        - IN_PROGRESS items → PENDING

        Returns:
            Number of jobs recovered.
        """
        result = await session.execute(
            select(UtilityJob).where(UtilityJob.state.in_([JobState.RUNNING, JobState.PAUSING]))
        )
        interrupted = list(result.scalars().all())

        for job in interrupted:
            job.state = JobState.PAUSED
            job.paused_at = datetime.now(UTC).isoformat()
            await project_utility_operation_progress(session, job)
            logger.info("job_recovered", job_id=job.id, old_state="RUNNING/PAUSING")

        # Reset any IN_PROGRESS items to PENDING
        await session.execute(
            update(UtilityJobItem)
            .where(UtilityJobItem.state == ItemState.IN_PROGRESS)
            .values(
                state=ItemState.PENDING,
                started_at=None,
                completed_at=None,
                worker_id=None,
            )
        )

        await session.flush()

        if interrupted:
            logger.info("startup_recovery_complete", jobs_recovered=len(interrupted))

        return len(interrupted)

    async def recover_and_dispatch(self) -> int:
        """Recover interrupted jobs, then restart serial dispatch for queued work."""
        async with self._session_factory() as session:
            recovered = await self.recover_interrupted_jobs(session)
            await session.commit()
        await self.dispatch_next()
        return recovered

    # ── Job Controls ───────────────────────────────────────────

    async def pause_job(self, session: AsyncSession, job_id: str) -> None:
        """Signal a running job to pause at the next item boundary.

        Transitions RUNNING → PAUSING. The execution loop checks this
        and completes the current item before transitioning to PAUSED.
        """
        job = await session.get(UtilityJob, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        self.transition(job, JobState.PAUSING)
        await session.flush()
        await project_utility_operation_progress(session, job)

    async def resume_job(self, session: AsyncSession, job_id: str) -> None:
        """Resume a paused job by re-queuing ahead of fresh work.

        Resumed jobs stay ahead of never-started queued jobs while
        preserving the existing order of already-resumed jobs.
        """
        job = await session.get(UtilityJob, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if JobState(job.state) != JobState.PAUSED:
            raise ValueError(f"Can only resume PAUSED jobs, got: {job.state}")

        queued_jobs = await queued_jobs_in_order(session)

        # Use the direct state machine for PAUSED → RUNNING is valid,
        # but we want PAUSED → QUEUED (re-queue). Since PAUSED doesn't
        # transition to QUEUED in the table, we handle resume specially:
        # place resumed jobs ahead of never-started queued jobs while
        # preserving existing resume order.
        job.state = JobState.QUEUED
        ordered_jobs = build_resume_queue_order(job, queued_jobs)
        await resequence_queued_jobs(session, ordered_jobs)
        await project_utility_operation_progress(session, job)

        logger.info("job_resumed", job_id=job_id, queue_position=job.queue_position)

    async def cancel_job(
        self,
        session: AsyncSession,
        job_id: str,
        rollback: bool = False,
    ) -> None:
        """Cancel a job. For QUEUED/PAUSED jobs, transitions directly to CANCELLED.

        For RUNNING jobs, transitions to CANCELLING (the execution loop
        finishes the current batch first).

        If rollback=True, creates a new rollback job with parent_job_id.
        """
        job = await session.get(UtilityJob, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        current = JobState(job.state)
        if current not in _CANCELLABLE_STATES:
            raise ValueError(
                f"Cannot cancel job in state {current.value}. "
                f"Cancellable states: {', '.join(s.value for s in _CANCELLABLE_STATES)}"
            )

        if current == JobState.RUNNING:
            # Running jobs go through CANCELLING first
            self.transition(job, JobState.CANCELLING)
        else:
            # QUEUED and PAUSED can go directly to CANCELLED
            # QUEUED → CANCELLING → CANCELLED (two-step)
            self.transition(job, JobState.CANCELLING)
            self.transition(job, JobState.CANCELLED)

        await session.flush()
        await project_utility_operation_progress(session, job)

        if rollback:
            await self._create_rollback_job(session, job)

        logger.info("job_cancelled", job_id=job_id, rollback=rollback)

    async def queue_rollback_job(
        self,
        session: AsyncSession,
        job_id: str,
        *,
        created_by: str | None = None,
    ) -> UtilityJob:
        """Queue a rollback child job for a completed or cancelled parent job."""
        job = await session.get(UtilityJob, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if job.job_type == JobType.ROLLBACK:
            raise ValueError("Cannot queue rollback for a rollback job")

        current = JobState(job.state)
        if current not in _ROLLBACKABLE_STATES:
            raise ValueError(f"Can only rollback COMPLETED or CANCELLED jobs, got: {current.value}")

        return await self._create_rollback_job(session, job, created_by=created_by)
