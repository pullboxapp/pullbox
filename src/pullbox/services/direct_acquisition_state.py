"""Deterministic state and progress rules for direct acquisition attempts."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pullbox.core.exceptions import ValidationError
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

ACQUISITION_TRANSITIONS: dict[DirectAcquisitionState, frozenset[DirectAcquisitionState]] = {
    DirectAcquisitionState.DISCOVERED: frozenset(
        {
            DirectAcquisitionState.PLANNED,
            DirectAcquisitionState.INTERVENTION,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.PLANNED: frozenset(
        {
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.RESOLVING,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.QUEUED: frozenset(
        {
            DirectAcquisitionState.RESOLVING,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.RESOLVING: frozenset(
        {
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.DOWNLOADING,
            DirectAcquisitionState.RETRY_PENDING,
            DirectAcquisitionState.INTERVENTION,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.DOWNLOADING: frozenset(
        {
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.PAUSED,
            DirectAcquisitionState.VALIDATING,
            DirectAcquisitionState.RETRY_PENDING,
            DirectAcquisitionState.INTERVENTION,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.PAUSED: frozenset(
        {
            DirectAcquisitionState.DOWNLOADING,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.VALIDATING: frozenset(
        {
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.POST_PROCESSING,
            DirectAcquisitionState.RETRY_PENDING,
            DirectAcquisitionState.INTERVENTION,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.POST_PROCESSING: frozenset(
        {
            DirectAcquisitionState.COMPLETED,
            DirectAcquisitionState.INTERVENTION,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.RETRY_PENDING: frozenset(
        {
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.RESOLVING,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.INTERVENTION: frozenset(
        {
            DirectAcquisitionState.PLANNED,
            DirectAcquisitionState.QUEUED,
            DirectAcquisitionState.RESOLVING,
            DirectAcquisitionState.POST_PROCESSING,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }
    ),
    DirectAcquisitionState.COMPLETED: frozenset(),
    DirectAcquisitionState.CANCELLED: frozenset(),
    DirectAcquisitionState.FAILED: frozenset(),
}

ARTIFACT_TRANSITIONS: dict[DirectArtifactState, frozenset[DirectArtifactState]] = {
    DirectArtifactState.PLANNED: frozenset(
        {
            DirectArtifactState.RESOLVING,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.RESOLVING: frozenset(
        {
            DirectArtifactState.TRANSFERRING,
            DirectArtifactState.RETRY_PENDING,
            DirectArtifactState.INTERVENTION,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.TRANSFERRING: frozenset(
        {
            DirectArtifactState.PAUSED,
            DirectArtifactState.VALIDATING,
            DirectArtifactState.RETRY_PENDING,
            DirectArtifactState.INTERVENTION,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.PAUSED: frozenset(
        {
            DirectArtifactState.TRANSFERRING,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.VALIDATING: frozenset(
        {
            DirectArtifactState.COMPLETED,
            DirectArtifactState.INTERVENTION,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.RETRY_PENDING: frozenset(
        {
            DirectArtifactState.RESOLVING,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.INTERVENTION: frozenset(
        {
            DirectArtifactState.RESOLVING,
            DirectArtifactState.VALIDATING,
            DirectArtifactState.CANCELLED,
            DirectArtifactState.FAILED,
        }
    ),
    DirectArtifactState.COMPLETED: frozenset(),
    DirectArtifactState.CANCELLED: frozenset(),
    DirectArtifactState.FAILED: frozenset(),
}

_ACQUISITION_TERMINAL_STATES = frozenset(
    {
        DirectAcquisitionState.COMPLETED,
        DirectAcquisitionState.CANCELLED,
        DirectAcquisitionState.FAILED,
    }
)
_ARTIFACT_TERMINAL_STATES = frozenset(
    {
        DirectArtifactState.COMPLETED,
        DirectArtifactState.CANCELLED,
        DirectArtifactState.FAILED,
    }
)


def transition_acquisition(
    attempt: DirectAcquisitionAttempt,
    new_state: DirectAcquisitionState,
    *,
    at: datetime | None = None,
) -> bool:
    """Apply one valid acquisition transition, returning whether state changed."""
    current = DirectAcquisitionState(attempt.state)
    if current is new_state:
        return False
    _validate_transition(
        kind="acquisition",
        current=current,
        new_state=new_state,
        allowed=ACQUISITION_TRANSITIONS[current],
    )

    timestamp = at or datetime.now(UTC)
    attempt.state = new_state
    if new_state is DirectAcquisitionState.RESOLVING and attempt.started_at is None:
        attempt.started_at = timestamp
    if new_state is DirectAcquisitionState.CANCELLED:
        attempt.cancelled_at = timestamp
    if new_state in _ACQUISITION_TERMINAL_STATES:
        attempt.completed_at = timestamp
    return True


def transition_artifact(
    artifact: DirectArtifactAttempt,
    new_state: DirectArtifactState,
    *,
    at: datetime | None = None,
) -> bool:
    """Apply one valid artifact transition, returning whether state changed."""
    current = DirectArtifactState(artifact.state)
    if current is new_state:
        return False
    _validate_transition(
        kind="artifact",
        current=current,
        new_state=new_state,
        allowed=ARTIFACT_TRANSITIONS[current],
    )

    artifact.state = new_state
    if new_state in _ARTIFACT_TERMINAL_STATES:
        artifact.completed_at = at or datetime.now(UTC)
    return True


def advance_acquisition_progress(
    attempt: DirectAcquisitionAttempt,
    *,
    revision: int,
    snapshot: Mapping[str, object],
) -> bool:
    """Persist a strictly newer progress snapshot or accept an exact replay."""
    current_revision = attempt.progress_revision
    proposed_snapshot = dict(snapshot)
    if revision == current_revision:
        if proposed_snapshot == attempt.progress_snapshot:
            return False
        raise ValidationError(f"Progress revision {revision} already has different data.")
    if revision < current_revision:
        raise ValidationError(f"Progress revision must be greater than {current_revision}.")

    attempt.progress_revision = revision
    attempt.progress_snapshot = proposed_snapshot
    return True


def reopen_terminal_acquisition_for_retry(
    attempt: DirectAcquisitionAttempt,
    artifact: DirectArtifactAttempt,
) -> None:
    """Reopen one explicitly retried terminal attempt with a fresh auto-retry budget."""
    acquisition_state = DirectAcquisitionState(attempt.state)
    artifact_state = DirectArtifactState(artifact.state)
    if acquisition_state not in {
        DirectAcquisitionState.FAILED,
        DirectAcquisitionState.CANCELLED,
    } or artifact_state not in {
        DirectArtifactState.FAILED,
        DirectArtifactState.CANCELLED,
    }:
        raise ValidationError("Only a terminal direct attempt can be explicitly retried.")

    # Explicit user retry starts a new bounded automatic retry cycle while
    # retaining the same durable plan and safe partial-transfer checkpoint.
    attempt.state = DirectAcquisitionState.RETRY_PENDING
    artifact.state = DirectArtifactState.RETRY_PENDING
    attempt.retry_count = 0
    artifact.retry_count = 0
    attempt.next_retry_at = None
    artifact.next_retry_at = None
    attempt.completed_at = None
    attempt.cancelled_at = None
    artifact.completed_at = None
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={
            "schema_version": 1,
            "stage": "retry_requested",
            "artifact_attempt_id": artifact.id,
        },
    )


def _validate_transition[StateT: enum.StrEnum](
    *,
    kind: str,
    current: StateT,
    new_state: StateT,
    allowed: frozenset[StateT],
) -> None:
    if new_state in allowed:
        return
    allowed_text = ", ".join(sorted(state.value for state in allowed)) or "none"
    raise ValidationError(
        f"Invalid direct {kind} transition from {current.value} to "
        f"{new_state.value}. Allowed: {allowed_text}."
    )
