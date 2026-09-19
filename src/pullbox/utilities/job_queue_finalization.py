"""Finalization helpers for utility queue dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pullbox.utilities.job_queue_completion import build_completion_decision, merge_finalize_result
from pullbox.utilities.job_queue_runtime_logs import persist_runtime_log_entries
from pullbox.utilities.job_queue_runtime_state import apply_job_counter_snapshot
from pullbox.utilities.job_queue_state import job_duration_seconds
from pullbox.utilities.models import JobState, UtilityJob

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pullbox.utilities.base_executor import FinalizeResult, JobExecutor, JobRunSummary
    from pullbox.utilities.job_queue_completion import CompletionDecision


@dataclass(frozen=True, slots=True)
class PausedDispatchFinalization:
    """Persisted message and logger context for a paused dispatch run."""

    message: str
    log_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompletedDispatchFinalization:
    """Merged completion decision and logger context for a finished dispatch run."""

    completion: CompletionDecision
    log_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchFinalizationResult:
    """Outcome and logger event for a dispatch finalization pass."""

    status: str
    log_event: str | None = None
    log_context: dict[str, Any] = field(default_factory=dict)


async def finalize_dispatch_job(
    session: Any,
    *,
    job_id: str,
    job_type: str,
    executor: JobExecutor,
    summary: JobRunSummary,
    config: dict[str, Any],
    job_context: dict[str, Any] | None,
    get_utility_log_level: Callable[[Any], Awaitable[str]],
    persist_log: Callable[..., None],
    transition_job: Callable[[UtilityJob, JobState], Any],
    project_progress: Callable[[Any, UtilityJob], Awaitable[None]] | None = None,
) -> DispatchFinalizationResult:
    """Persist final counters, logs, executor finalization, and terminal state."""
    job = await session.get(UtilityJob, job_id)
    if job is None:
        return DispatchFinalizationResult(status="missing")

    utility_log_level = await get_utility_log_level(session)
    summary.metadata["utility_log_level"] = utility_log_level
    apply_job_counter_snapshot(
        job,
        completed=summary.completed,
        failed=summary.failed,
        skipped=summary.skipped,
        warnings=summary.warnings,
    )

    current = JobState(job.state)
    if current == JobState.PAUSING:
        transition_job(job, JobState.PAUSED)
        current = JobState.PAUSED
    if current == JobState.PAUSED:
        paused_finalization = persist_paused_dispatch_log(
            session,
            job_id=job.id,
            summary=summary,
            configured_level=utility_log_level,
            persist_log=persist_log,
        )
        if project_progress is not None:
            await project_progress(session, job)
        await session.commit()
        return DispatchFinalizationResult(
            status="paused",
            log_event="job_dispatch_paused",
            log_context=paused_finalization.log_context,
        )

    completion = build_completion_decision(
        current=current,
        job_type=job_type,
        summary=summary,
    )
    transition_job(job, completion.target_state)
    if completion.error_message:
        job.error_message = completion.error_message

    finalize_result = await executor.finalize_job(
        session,
        job,
        summary,
        config,
        job_context,
    )
    persist_runtime_log_entries(
        session,
        runtime_logs=finalize_result.extra_logs,
        persist_log=persist_log,
        configured_level=utility_log_level,
        job_id=job.id,
    )
    completed_finalization = persist_completed_dispatch_log(
        session,
        job=job,
        summary=summary,
        completion=completion,
        finalize_result=finalize_result,
        configured_level=utility_log_level,
        persist_log=persist_log,
    )

    if project_progress is not None:
        await project_progress(session, job)
    await session.commit()
    return DispatchFinalizationResult(
        status="completed",
        log_event="job_dispatch_completed",
        log_context=completed_finalization.log_context,
    )


def persist_completed_dispatch_log(
    session: Any,
    *,
    job: UtilityJob,
    summary: JobRunSummary,
    completion: CompletionDecision,
    finalize_result: FinalizeResult,
    configured_level: str,
    persist_log: Callable[..., None],
) -> CompletedDispatchFinalization:
    """Persist and describe the standard completed-dispatch log entry."""
    if finalize_result.error_message:
        job.error_message = finalize_result.error_message
    merged_completion = merge_finalize_result(completion, finalize_result)
    persist_log(
        session,
        configured_level=configured_level,
        job_id=job.id,
        level=merged_completion.log_level,
        message=merged_completion.message,
    )
    return CompletedDispatchFinalization(
        completion=merged_completion,
        log_context={
            "job_id": job.id,
            "job_type": job.job_type,
            "final_state": job.state,
            "completed": summary.completed,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "warnings": summary.warnings,
            "duration_seconds": job_duration_seconds(job),
        },
    )


def persist_paused_dispatch_log(
    session: Any,
    *,
    job_id: str,
    summary: JobRunSummary,
    configured_level: str,
    persist_log: Callable[..., None],
) -> PausedDispatchFinalization:
    """Persist and describe the standard paused-dispatch log entry."""
    message = (
        f"Job paused. {summary.completed} completed, "
        f"{summary.failed} failed, {summary.skipped} skipped."
    )
    persist_log(
        session,
        configured_level=configured_level,
        job_id=job_id,
        level="INFO",
        message=message,
    )
    return PausedDispatchFinalization(
        message=message,
        log_context={
            "job_id": job_id,
            "completed": summary.completed,
            "failed": summary.failed,
            "skipped": summary.skipped,
        },
    )
