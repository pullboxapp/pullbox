"""State-machine helpers for utility job queue lifecycle transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pullbox.utilities.models import JobState

VALID_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLING, JobState.FAILED},
    JobState.RUNNING: {
        JobState.COMPLETED,
        JobState.PAUSING,
        JobState.CANCELLING,
        JobState.FAILED,
    },
    JobState.PAUSING: {JobState.PAUSED, JobState.CANCELLING, JobState.FAILED},
    JobState.PAUSED: {JobState.RUNNING, JobState.CANCELLING},
    JobState.CANCELLING: {JobState.CANCELLED, JobState.FAILED},
    JobState.CANCELLED: {JobState.ROLLING_BACK},
    JobState.COMPLETED: {JobState.ROLLING_BACK},
    JobState.FAILED: set(),
    JobState.ROLLING_BACK: {JobState.ROLLED_BACK, JobState.FAILED},
    JobState.ROLLED_BACK: set(),
}

TERMINAL_STATES = frozenset(
    {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ROLLED_BACK,
    }
)

CANCELLABLE_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.PAUSED,
        JobState.PAUSING,
    }
)

ROLLBACKABLE_STATES = frozenset(
    {
        JobState.COMPLETED,
        JobState.CANCELLED,
    }
)


def transition_job_state(job: Any, new_state: JobState) -> JobState:
    """Enforce valid queue-state transitions and update lifecycle timestamps."""
    current = JobState(job.state)
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_state not in allowed:
        allowed_str = ", ".join(s.value for s in allowed) or "none (terminal)"
        raise ValueError(
            f"Invalid transition: {current.value} \u2192 {new_state.value}. Allowed: {allowed_str}"
        )

    job.state = new_state.value

    now = datetime.now(UTC).isoformat()
    if new_state == JobState.RUNNING:
        job.started_at = now
    elif new_state == JobState.PAUSED:
        job.paused_at = now
    elif new_state in TERMINAL_STATES:
        job.completed_at = now
        job.queue_position = None

    return current


def job_duration_seconds(job: Any) -> float | None:
    """Return job runtime in seconds when both endpoints are available."""
    if not job.started_at or not job.completed_at:
        return None
    try:
        started_at = datetime.fromisoformat(job.started_at)
        completed_at = datetime.fromisoformat(job.completed_at)
    except ValueError:
        return None
    return round(max((completed_at - started_at).total_seconds(), 0.0), 3)
