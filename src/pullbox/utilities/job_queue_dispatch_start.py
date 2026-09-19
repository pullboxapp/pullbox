"""Helpers for selecting and starting utility queue dispatch work."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.utilities.job_queue_state import transition_job_state
from pullbox.utilities.models import JobState, UtilityJob

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.utilities.base_executor import JobExecutor


@dataclass(frozen=True, slots=True)
class StartedDispatchJob:
    """Runtime data needed after a queued job has been started."""

    job_id: str
    job_type: str
    display_name: str
    raw_config: Any
    executor: JobExecutor


@dataclass(frozen=True, slots=True)
class StartedDispatchTransition:
    """Persisted start transition and logger context for dispatch."""

    started_job: StartedDispatchJob
    log_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchStartResult:
    """Queue dispatch start outcome and optional logger event."""

    status: str
    started_job: StartedDispatchJob | None = None
    log_event: str | None = None
    log_context: dict[str, Any] = field(default_factory=dict)


async def load_next_dispatch_candidate(session: AsyncSession) -> UtilityJob | None:
    """Return the next queued job only when no job is already running."""
    running = await session.execute(
        select(UtilityJob)
        .where(UtilityJob.state.in_([JobState.RUNNING, JobState.PAUSING, JobState.CANCELLING]))
        .limit(1)
    )
    if running.scalar_one_or_none() is not None:
        return None

    result = await session.execute(
        select(UtilityJob)
        .where(UtilityJob.state == JobState.QUEUED)
        .order_by(UtilityJob.queue_position, UtilityJob.created_at, UtilityJob.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


def mark_job_without_executor(job: UtilityJob, *, completed_at: str) -> None:
    """Mark a queued job failed because no executor is registered for its type."""
    job.state = JobState.FAILED
    job.error_message = f"No executor registered for job type: {job.job_type}"
    job.completed_at = completed_at


def mark_job_started(job: UtilityJob, *, started_at: str) -> None:
    """Transition a queued job into its running dispatch state."""
    transition_job_state(job, JobState.RUNNING)
    job.started_at = started_at
    job.queue_position = None


def snapshot_started_job(job: UtilityJob, executor: JobExecutor) -> StartedDispatchJob:
    """Capture runtime fields before the session that started the job closes."""
    return StartedDispatchJob(
        job_id=job.id,
        job_type=job.job_type,
        display_name=job.display_name,
        raw_config=job.config,
        executor=executor,
    )


async def persist_started_dispatch_job(
    session: Any,
    *,
    job: UtilityJob,
    executor: JobExecutor,
    utility_log_level: str,
    persist_log: Callable[..., None],
    started_at: str,
) -> StartedDispatchTransition:
    """Persist the transition from queued to running and return runtime data."""
    mark_job_started(job, started_at=started_at)
    persist_log(
        session,
        configured_level=utility_log_level,
        job_id=job.id,
        level="INFO",
        message=f"Job started: {job.display_name}",
    )
    await session.commit()
    return StartedDispatchTransition(
        started_job=snapshot_started_job(job, executor),
        log_context={
            "job_id": job.id,
            "job_type": job.job_type,
            "display_name": job.display_name,
        },
    )


async def start_next_dispatch_job(
    session: AsyncSession,
    *,
    get_executor: Callable[[str], JobExecutor | None],
    get_utility_log_level: Callable[[Any], Awaitable[str]],
    persist_log: Callable[..., None],
    timestamp_factory: Callable[[], str],
) -> DispatchStartResult:
    """Select and persist the next queued utility job, if one can start."""
    job = await load_next_dispatch_candidate(session)
    if job is None:
        return DispatchStartResult(
            status="idle",
            log_context={},
        )

    executor = get_executor(job.job_type)
    if executor is None:
        mark_job_without_executor(
            job,
            completed_at=timestamp_factory(),
        )
        await session.commit()
        return DispatchStartResult(
            status="missing_executor",
            log_context={},
        )

    utility_log_level = await get_utility_log_level(session)
    started_transition = await persist_started_dispatch_job(
        session,
        job=job,
        executor=executor,
        utility_log_level=utility_log_level,
        persist_log=persist_log,
        started_at=timestamp_factory(),
    )
    return DispatchStartResult(
        status="started",
        started_job=started_transition.started_job,
        log_event="job_dispatch_started",
        log_context=started_transition.log_context,
    )
