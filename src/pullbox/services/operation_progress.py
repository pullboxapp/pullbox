"""Shared revisioned progress projection for background operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class OperationProgressMeasure:
    """One determinate or indeterminate progress measurement."""

    current: int | None = None
    total: int | None = None
    percent: float | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class OperationItemProgress:
    """Current item nested beneath an operation's overall progress."""

    key: str
    label: str
    phase: str
    message: str = ""
    measure: OperationProgressMeasure = field(default_factory=OperationProgressMeasure)


@dataclass(frozen=True, slots=True)
class OperationProgressUpdate:
    """Complete latest-state update for one operation projection row."""

    operation_type: OperationProgressType
    operation_key: str
    revision: int | None
    state: OperationProgressState
    phase: str
    title: str
    message: str = ""
    source_label: str | None = None
    detail_url: str | None = None
    group_key: str | None = None
    visibility: OperationProgressVisibility = OperationProgressVisibility.PROMINENT
    tone: OperationProgressTone = OperationProgressTone.INFO
    attention_required: bool = False
    overall: OperationProgressMeasure = field(default_factory=OperationProgressMeasure)
    item: OperationItemProgress | None = None
    rate: float | None = None
    rate_unit: str | None = None
    eta_seconds: int | None = None
    detail_snapshot: dict[str, object] = field(default_factory=dict)
    started_at: datetime | None = None
    event_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OperationProgressPublishResult:
    """Result of applying one revisioned projection update."""

    operation: OperationProgress
    accepted: bool


@dataclass(frozen=True, slots=True)
class _NormalizedMeasure:
    current: int | None
    total: int | None
    percent: float | None
    unit: str | None
    indeterminate: bool


def _clamp_percent(value: float) -> float:
    return round(min(max(float(value), 0.0), 100.0), 1)


def _normalize_measure(measure: OperationProgressMeasure) -> _NormalizedMeasure:
    current = max(int(measure.current), 0) if measure.current is not None else None
    total = max(int(measure.total), 0) if measure.total is not None else None
    if total == 0:
        total = None

    percent = measure.percent
    if percent is None and current is not None and total is not None:
        percent = (min(current, total) / total) * 100
    normalized_percent = _clamp_percent(percent) if percent is not None else None
    return _NormalizedMeasure(
        current=current,
        total=total,
        percent=normalized_percent,
        unit=measure.unit,
        indeterminate=normalized_percent is None,
    )


async def publish_operation_progress(
    session: AsyncSession,
    update: OperationProgressUpdate,
) -> OperationProgressPublishResult:
    """Apply one newest-wins update to the durable progress projection."""
    existing = (
        await session.execute(
            select(OperationProgress)
            .where(
                OperationProgress.operation_type == update.operation_type,
                OperationProgress.operation_key == update.operation_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    revision = update.revision
    if revision is None:
        revision = (existing.revision if existing is not None else 0) + 1
    if revision < 0:
        raise ValueError("Operation progress revision cannot be negative.")
    if existing is not None and revision <= existing.revision:
        return OperationProgressPublishResult(operation=existing, accepted=False)

    previous_attention_required = existing.attention_required if existing is not None else False
    previous_state = existing.state if existing is not None else None
    previous_phase = existing.phase if existing is not None else None
    previous_message = existing.message if existing is not None else None
    previous_detail_snapshot = existing.detail_snapshot if existing is not None else None
    previous_acknowledged_at = existing.acknowledged_at if existing is not None else None
    now = update.event_at or datetime.now(UTC)
    overall = _normalize_measure(update.overall)
    item = _normalize_measure(update.item.measure) if update.item is not None else None
    if existing is None:
        existing = OperationProgress(
            operation_type=update.operation_type,
            operation_key=update.operation_key,
            state=update.state,
            phase=update.phase,
            title=update.title,
            revision=revision,
            last_event_at=now,
        )
        session.add(existing)

    previous_overall_percent = existing.overall_percent
    if (
        previous_overall_percent is not None
        and overall.percent is not None
        and overall.percent < previous_overall_percent
        and not update.state.is_terminal
    ):
        overall = _NormalizedMeasure(
            current=(
                max(existing.overall_current or 0, overall.current)
                if overall.current is not None
                else existing.overall_current
            ),
            total=overall.total or existing.overall_total,
            percent=previous_overall_percent,
            unit=overall.unit or existing.overall_unit,
            indeterminate=False,
        )

    same_item_phase = bool(
        update.item is not None
        and existing.item_key == update.item.key
        and existing.item_phase == update.item.phase
    )
    if (
        same_item_phase
        and existing.item_percent is not None
        and item is not None
        and item.percent is not None
        and item.percent < existing.item_percent
    ):
        item = _NormalizedMeasure(
            current=(
                max(existing.item_current or 0, item.current)
                if item.current is not None
                else existing.item_current
            ),
            total=item.total or existing.item_total,
            percent=existing.item_percent,
            unit=item.unit or existing.item_unit,
            indeterminate=False,
        )

    if update.state is OperationProgressState.COMPLETED:
        overall = _NormalizedMeasure(
            current=overall.total if overall.total is not None else overall.current,
            total=overall.total,
            percent=100.0,
            unit=overall.unit,
            indeterminate=False,
        )

    existing.group_key = update.group_key
    existing.state = update.state
    existing.phase = update.phase
    existing.title = update.title
    existing.message = update.message
    existing.source_label = update.source_label
    existing.detail_url = update.detail_url
    existing.visibility = update.visibility
    existing.tone = update.tone
    existing.attention_required = update.attention_required
    materially_new_attention = bool(
        update.attention_required
        and previous_acknowledged_at is not None
        and (
            not previous_attention_required
            or previous_state != update.state
            or previous_phase != update.phase
            or previous_message != update.message
            or previous_detail_snapshot != update.detail_snapshot
        )
    )
    if not update.attention_required or materially_new_attention:
        existing.acknowledged_at = None
    existing.revision = revision
    existing.overall_current = overall.current
    existing.overall_total = overall.total
    existing.overall_percent = overall.percent
    existing.overall_unit = overall.unit
    existing.overall_indeterminate = overall.indeterminate
    existing.rate = max(float(update.rate), 0.0) if update.rate is not None else None
    existing.rate_unit = update.rate_unit
    existing.eta_seconds = (
        max(int(update.eta_seconds), 0) if update.eta_seconds is not None else None
    )
    existing.detail_snapshot = dict(update.detail_snapshot)
    existing.started_at = update.started_at or existing.started_at or now
    existing.completed_at = now if update.state.is_terminal else None
    existing.last_event_at = now

    if update.item is None:
        existing.item_key = None
        existing.item_label = None
        existing.item_phase = None
        existing.item_message = None
        existing.item_current = None
        existing.item_total = None
        existing.item_percent = None
        existing.item_unit = None
        existing.item_indeterminate = True
    else:
        assert item is not None
        existing.item_key = update.item.key
        existing.item_label = update.item.label
        existing.item_phase = update.item.phase
        existing.item_message = update.item.message
        existing.item_current = item.current
        existing.item_total = item.total
        existing.item_percent = item.percent
        existing.item_unit = item.unit
        existing.item_indeterminate = item.indeterminate

    await session.flush()
    return OperationProgressPublishResult(operation=existing, accepted=True)
