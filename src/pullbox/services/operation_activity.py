"""Queries and acknowledgement behavior for global operation activity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.orm import aliased

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
    total_count: int
    refresh_after_ms: int | None


async def list_operation_activity(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
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
        OperationProgress.completed_at > recent_cutoff,
        OperationProgress.visibility != OperationProgressVisibility.QUIET,
    )
    visible_clause = or_(active_clause, unacknowledged_attention_clause, recent_terminal_clause)
    attention_rank = case((unacknowledged_attention_clause, 0), else_=1)
    active_rank = case((active_clause, 0), else_=1)
    page = (
        select(
            OperationProgress,
            attention_rank.label("attention_rank"),
            active_rank.label("active_rank"),
        )
        .where(visible_clause)
        .order_by(
            attention_rank,
            active_rank,
            OperationProgress.updated_at.desc(),
            OperationProgress.id.desc(),
        )
        .limit(max(1, min(limit, 100)))
        .offset(max(0, offset))
        .subquery()
    )
    counts = (
        select(
            func.count(OperationProgress.id).filter(active_clause).label("active_count"),
            func.count(OperationProgress.id)
            .filter(
                OperationProgress.state.in_(_SPINNER_STATES),
                OperationProgress.visibility == OperationProgressVisibility.PROMINENT,
            )
            .label("spinner_count"),
            func.count(OperationProgress.id)
            .filter(
                unacknowledged_attention_clause,
            )
            .label("attention_count"),
            func.count(OperationProgress.id).label("total_count"),
            func.min(OperationProgress.completed_at)
            .filter(
                recent_terminal_clause,
                ~unacknowledged_attention_clause,
            )
            .label("earliest_completion"),
        )
        .where(visible_clause)
        .subquery()
    )
    # One statement keeps counters, rows, and expiry on the same database snapshot,
    # including an empty page after work completes (SQLite and PostgreSQL).
    paged_operation = aliased(OperationProgress, page)
    rows = (
        await session.execute(
            select(paged_operation, counts)
            .select_from(counts.outerjoin(page, true()))
            .order_by(
                page.c.attention_rank,
                page.c.active_rank,
                page.c.updated_at.desc(),
                page.c.id.desc(),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    count_row = rows[0]
    earliest = count_row.earliest_completion
    refresh_after_ms = None
    if earliest is not None:
        earliest = earliest.replace(tzinfo=UTC) if earliest.tzinfo is None else earliest
        refresh_after_ms = max(
            1, ceil((earliest + _COMPLETION_GRACE - current_time).total_seconds() * 1000)
        )
    return OperationActivity(
        operations=[row[0] for row in rows if row[0] is not None],
        active_count=count_row.active_count,
        spinner_count=count_row.spinner_count,
        attention_count=count_row.attention_count,
        total_count=count_row.total_count,
        refresh_after_ms=refresh_after_ms,
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
