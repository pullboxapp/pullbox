"""Queries and acknowledgement behavior for global operation activity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, or_, select

from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressVisibility,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_ACTIVE_STATES = frozenset(
    {
        OperationProgressState.QUEUED,
        OperationProgressState.RUNNING,
        OperationProgressState.PAUSED,
        OperationProgressState.RETRYING,
    }
)
_SPINNER_STATES = frozenset(
    {
        OperationProgressState.QUEUED,
        OperationProgressState.RUNNING,
        OperationProgressState.RETRYING,
    }
)
_COMPLETION_GRACE = timedelta(seconds=15)


@dataclass(frozen=True, slots=True)
class OperationActivity:
    """Visible global activity plus aggregate shell counters."""

    operations: list[OperationProgress]
    active_count: int
    spinner_count: int
    attention_count: int


async def list_operation_activity(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> OperationActivity:
    """Return user-visible work without promoting successful routine jobs."""
    current_time = now or datetime.now(UTC)
    recent_cutoff = current_time - _COMPLETION_GRACE
    active_clause = and_(
        OperationProgress.state.in_(_ACTIVE_STATES),
        or_(
            OperationProgress.visibility != OperationProgressVisibility.QUIET,
            OperationProgress.attention_required.is_(True),
        ),
    )
    unacknowledged_attention_clause = and_(
        OperationProgress.attention_required.is_(True),
        OperationProgress.acknowledged_at.is_(None),
    )
    recent_terminal_clause = and_(
        OperationProgress.state.not_in(_ACTIVE_STATES),
        OperationProgress.completed_at.is_not(None),
        OperationProgress.completed_at >= recent_cutoff,
        OperationProgress.visibility != OperationProgressVisibility.QUIET,
    )
    result = await session.execute(
        select(OperationProgress)
        .where(
            or_(
                active_clause,
                unacknowledged_attention_clause,
                recent_terminal_clause,
            )
        )
        .order_by(
            case((unacknowledged_attention_clause, 0), else_=1),
            case((active_clause, 0), else_=1),
            OperationProgress.updated_at.desc(),
            OperationProgress.id.desc(),
        )
        .limit(max(1, min(limit, 100)))
    )
    operations = list(result.scalars().all())
    active_count = sum(1 for item in operations if item.state in _ACTIVE_STATES)
    spinner_count = sum(
        1
        for item in operations
        if item.state in _SPINNER_STATES
        and item.visibility is OperationProgressVisibility.PROMINENT
    )
    attention_count = sum(
        1 for item in operations if item.attention_required and item.acknowledged_at is None
    )
    return OperationActivity(
        operations=operations,
        active_count=active_count,
        spinner_count=spinner_count,
        attention_count=attention_count,
    )


async def acknowledge_operation(
    session: AsyncSession,
    operation_id: int,
    *,
    at: datetime | None = None,
) -> OperationProgress | None:
    """Acknowledge one operation's attention state without deleting history."""
    operation = await session.get(OperationProgress, operation_id)
    if operation is None:
        return None
    operation.acknowledged_at = at or datetime.now(UTC)
    await session.flush()
    return operation
