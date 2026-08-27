"""Persistence helpers for health check results."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.database import get_session_factory
from pullbox.models.health import HealthCheckResult as HealthCheckResultModel
from pullbox.models.health import HealthCurrentStatus as HealthCurrentStatusModel
from pullbox.models.health import HealthIncident as HealthIncidentModel
from pullbox.models.health import HealthStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.health_types import CheckOutcome

logger = structlog.get_logger(__name__)

_CURRENT_SUMMARY_KEY = "__summary__"
_INCIDENT_STATUSES = frozenset({HealthStatus.DEGRADED, HealthStatus.UNHEALTHY})


@dataclass(frozen=True)
class _CurrentHealthPayload:
    component: str
    current_key: str
    check_name: str
    subject_key: str | None
    subject_key_norm: str
    subject_label: str | None
    status: HealthStatus
    message: str | None
    details_json: str | None
    response_time_ms: float | None
    run_id: str
    is_summary: bool
    checked_at: datetime


async def get_overall_health_status(session: AsyncSession) -> HealthStatus:
    """Return the worst status across current top-level component summaries."""
    result = await session.execute(
        select(HealthCurrentStatusModel.status).where(
            HealthCurrentStatusModel.is_summary.is_(True),
            HealthCurrentStatusModel.subject_key_norm == "",
        )
    )
    statuses = list(result.scalars().all())

    if not statuses:
        return HealthStatus.UNKNOWN

    precedence = {
        HealthStatus.UNHEALTHY: 3,
        HealthStatus.DEGRADED: 2,
        HealthStatus.UNKNOWN: 1,
        HealthStatus.HEALTHY: 0,
    }
    worst: HealthStatus = max(statuses, key=lambda status: precedence.get(status, 0))
    return worst


async def get_health_history(
    session: AsyncSession,
    component: str | None = None,
    limit: int = 50,
    *,
    is_summary: bool | None = None,
    subject_key: str | None = None,
) -> list[HealthCheckResultModel]:
    """Retrieve recent health check results, optionally filtered by component."""
    stmt = select(HealthCheckResultModel).order_by(HealthCheckResultModel.checked_at.desc())
    if component:
        stmt = stmt.where(HealthCheckResultModel.component == component)
    if is_summary is not None:
        stmt = stmt.where(HealthCheckResultModel.is_summary.is_(is_summary))
    if subject_key is None:
        stmt = stmt.where(HealthCheckResultModel.subject_key.is_(None))
    else:
        stmt = stmt.where(HealthCheckResultModel.subject_key == subject_key)
    stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_health_incidents(
    session: AsyncSession,
    component: str | None = None,
    limit: int = 50,
    *,
    include_resolved: bool = True,
) -> list[HealthIncidentModel]:
    """Retrieve compact long-term health incidents."""
    stmt = select(HealthIncidentModel)
    if component:
        stmt = stmt.where(HealthIncidentModel.component == component)
    if not include_resolved:
        stmt = stmt.where(HealthIncidentModel.resolved_at.is_(None))
    stmt = stmt.order_by(
        HealthIncidentModel.resolved_at.is_not(None),
        HealthIncidentModel.last_seen_at.desc(),
        HealthIncidentModel.id.desc(),
    ).limit(limit)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def cleanup_health_history(
    session: AsyncSession,
    retention_days: int,
) -> int:
    """Delete health check results older than *retention_days*."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    stmt = delete(HealthCheckResultModel).where(HealthCheckResultModel.checked_at < cutoff)
    cursor = await session.execute(stmt)
    deleted: int = cursor.rowcount  # type: ignore[attr-defined]
    return deleted


async def should_skip_comicvine_check(
    session: AsyncSession,
    *,
    interval_minutes: int,
) -> bool:
    """Return True if the last ComicVine summary check is recent enough to reuse."""
    cutoff = datetime.now(UTC) - timedelta(minutes=interval_minutes)
    stmt = select(HealthCurrentStatusModel.checked_at).where(
        HealthCurrentStatusModel.component == "comicvine",
        HealthCurrentStatusModel.is_summary.is_(True),
        HealthCurrentStatusModel.subject_key_norm == "",
    )
    result = await session.execute(stmt)
    last_checked: datetime | None = result.scalar_one_or_none()
    if last_checked is None:
        return False
    cutoff_naive = cutoff.replace(tzinfo=None)
    checked_naive = last_checked.replace(tzinfo=None)
    return checked_naive >= cutoff_naive


async def persist_health_outcomes(
    session: AsyncSession,
    outcomes: list[CheckOutcome],
    *,
    session_factory_provider: Callable[[], Any] = get_session_factory,
    retry_delay: Callable[[int], float] = sqlite_lock_retry_delay,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    lock_retry_attempts: int = SQLITE_LOCK_RETRY_ATTEMPTS,
) -> None:
    """Write check outcomes to history and current-state tables."""
    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex
    bind = getattr(session, "bind", None)
    factory = (
        async_sessionmaker(bind=bind, expire_on_commit=False)
        if bind is not None
        else session_factory_provider()
    )
    for attempt in range(1, lock_retry_attempts + 1):
        active_session = session
        manage_commit = False
        if attempt > 1:
            active_session = await factory().__aenter__()
            manage_commit = True
        try:
            stage_health_outcome_rows(
                active_session,
                outcomes,
                run_id=run_id,
                checked_at=now,
            )
            await upsert_health_current_status_rows(
                active_session,
                outcomes,
                run_id=run_id,
                checked_at=now,
            )
            await update_health_incidents(
                active_session,
                outcomes,
                run_id=run_id,
                checked_at=now,
            )
            await active_session.flush()
            if manage_commit:
                await active_session.commit()
                await session.commit()
            return
        except OperationalError as exc:
            await active_session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == lock_retry_attempts:
                raise
            delay_seconds = retry_delay(attempt)
            logger.warning(
                "health_result_persist_retrying_after_sqlite_lock",
                attempt=attempt,
                max_attempts=lock_retry_attempts,
                delay_seconds=delay_seconds,
            )
        finally:
            if manage_commit:
                await active_session.__aexit__(None, None, None)
        await sleep(delay_seconds)


def stage_health_outcome_rows(
    session: AsyncSession,
    outcomes: list[CheckOutcome],
    *,
    run_id: str,
    checked_at: datetime,
) -> None:
    """Add health outcome rows to the current session transaction."""
    for outcome in outcomes:
        summary_row = HealthCheckResultModel(
            component=outcome.component,
            check_name=outcome.check_name,
            subject_key=outcome.subject_key,
            subject_label=outcome.subject_label,
            status=outcome.status,
            message=outcome.message,
            details_json=_details_to_json(outcome.details),
            response_time_ms=outcome.response_time_ms,
            run_id=run_id,
            is_summary=True,
            checked_at=checked_at,
        )
        session.add(summary_row)
        for sub_check in outcome.sub_checks:
            session.add(
                HealthCheckResultModel(
                    component=outcome.component,
                    check_name=sub_check.check_name,
                    subject_key=sub_check.subject_key or outcome.subject_key,
                    subject_label=sub_check.subject_label or outcome.subject_label,
                    status=sub_check.status,
                    message=sub_check.message,
                    details_json=_details_to_json(sub_check.details),
                    response_time_ms=sub_check.response_time_ms,
                    run_id=run_id,
                    is_summary=False,
                    checked_at=checked_at,
                )
            )


async def upsert_health_current_status_rows(
    session: AsyncSession,
    outcomes: list[CheckOutcome],
    *,
    run_id: str,
    checked_at: datetime,
) -> None:
    """Upsert bounded current-state rows for health outcomes."""
    payloads = _current_health_payloads(outcomes, run_id=run_id, checked_at=checked_at)
    if not payloads:
        return

    components = sorted({payload.component for payload in payloads})
    result = await session.execute(
        select(HealthCurrentStatusModel).where(HealthCurrentStatusModel.component.in_(components))
    )
    existing = {_current_status_identity(row): row for row in result.scalars().all()}
    current_identities = {
        (payload.component, payload.subject_key_norm, payload.current_key) for payload in payloads
    }

    for identity, stale_row in existing.items():
        if identity not in current_identities:
            await session.delete(stale_row)

    for payload in payloads:
        identity = (payload.component, payload.subject_key_norm, payload.current_key)
        row = existing.get(identity)
        if row is None:
            row = HealthCurrentStatusModel(
                component=payload.component,
                current_key=payload.current_key,
                check_name=payload.check_name,
                subject_key=payload.subject_key,
                subject_key_norm=payload.subject_key_norm,
                subject_label=payload.subject_label,
                status=payload.status,
                message=payload.message,
                details_json=payload.details_json,
                response_time_ms=payload.response_time_ms,
                run_id=payload.run_id,
                is_summary=payload.is_summary,
                checked_at=payload.checked_at,
            )
            session.add(row)
            existing[identity] = row
        else:
            _apply_current_health_payload(row, payload)


async def update_health_incidents(
    session: AsyncSession,
    outcomes: list[CheckOutcome],
    *,
    run_id: str,
    checked_at: datetime,
) -> None:
    """Open, update, or resolve compact incident rows from summary outcomes."""
    payloads = [
        payload
        for payload in _current_health_payloads(outcomes, run_id=run_id, checked_at=checked_at)
        if payload.is_summary
    ]
    if not payloads:
        return

    components = sorted({payload.component for payload in payloads})
    result = await session.execute(
        select(HealthIncidentModel).where(
            HealthIncidentModel.component.in_(components),
            HealthIncidentModel.resolved_at.is_(None),
        )
    )
    active = {_incident_identity(row): row for row in result.scalars().all()}
    current_identities = {
        (payload.component, payload.subject_key_norm, payload.current_key) for payload in payloads
    }

    for identity, retired_incident in active.items():
        if identity not in current_identities:
            retired_incident.resolved_at = checked_at

    for payload in payloads:
        identity = (payload.component, payload.subject_key_norm, payload.current_key)
        incident = active.get(identity)
        if payload.status in _INCIDENT_STATUSES:
            if incident is None:
                incident = HealthIncidentModel(
                    component=payload.component,
                    current_key=payload.current_key,
                    check_name=payload.check_name,
                    subject_key=payload.subject_key,
                    subject_key_norm=payload.subject_key_norm,
                    subject_label=payload.subject_label,
                    status=payload.status,
                    is_summary=payload.is_summary,
                    first_seen_at=payload.checked_at,
                    last_seen_at=payload.checked_at,
                    occurrence_count=1,
                    last_message=payload.message,
                    last_details_json=payload.details_json,
                    last_response_time_ms=payload.response_time_ms,
                    last_run_id=payload.run_id,
                )
                session.add(incident)
                active[identity] = incident
            else:
                incident.check_name = payload.check_name
                incident.subject_key = payload.subject_key
                incident.subject_key_norm = payload.subject_key_norm
                incident.subject_label = payload.subject_label
                incident.status = payload.status
                incident.last_seen_at = payload.checked_at
                incident.occurrence_count += 1
                incident.last_message = payload.message
                incident.last_details_json = payload.details_json
                incident.last_response_time_ms = payload.response_time_ms
                incident.last_run_id = payload.run_id
        elif payload.status == HealthStatus.HEALTHY and incident is not None:
            incident.resolved_at = checked_at


def _current_health_payloads(
    outcomes: list[CheckOutcome],
    *,
    run_id: str,
    checked_at: datetime,
) -> list[_CurrentHealthPayload]:
    """Build current-state payloads from summary and sub-check outcomes."""
    payloads: list[_CurrentHealthPayload] = []
    for outcome in outcomes:
        payloads.append(
            _CurrentHealthPayload(
                component=outcome.component,
                current_key=_CURRENT_SUMMARY_KEY,
                check_name=outcome.check_name,
                subject_key=outcome.subject_key,
                subject_key_norm=_subject_key_norm(outcome.subject_key),
                subject_label=outcome.subject_label,
                status=outcome.status,
                message=outcome.message,
                details_json=_details_to_json(outcome.details),
                response_time_ms=outcome.response_time_ms,
                run_id=run_id,
                is_summary=True,
                checked_at=checked_at,
            )
        )
        for sub_check in outcome.sub_checks:
            subject_key = sub_check.subject_key or outcome.subject_key
            payloads.append(
                _CurrentHealthPayload(
                    component=outcome.component,
                    current_key=sub_check.check_name,
                    check_name=sub_check.check_name,
                    subject_key=subject_key,
                    subject_key_norm=_subject_key_norm(subject_key),
                    subject_label=sub_check.subject_label or outcome.subject_label,
                    status=sub_check.status,
                    message=sub_check.message,
                    details_json=_details_to_json(sub_check.details),
                    response_time_ms=sub_check.response_time_ms,
                    run_id=run_id,
                    is_summary=False,
                    checked_at=checked_at,
                )
            )
    return payloads


def _apply_current_health_payload(
    row: HealthCurrentStatusModel,
    payload: _CurrentHealthPayload,
) -> None:
    """Apply latest outcome data to an existing current-state row."""
    row.check_name = payload.check_name
    row.subject_key = payload.subject_key
    row.subject_key_norm = payload.subject_key_norm
    row.subject_label = payload.subject_label
    row.status = payload.status
    row.message = payload.message
    row.details_json = payload.details_json
    row.response_time_ms = payload.response_time_ms
    row.run_id = payload.run_id
    row.is_summary = payload.is_summary
    row.checked_at = payload.checked_at


def _current_status_identity(row: HealthCurrentStatusModel) -> tuple[str, str, str]:
    """Return the unique identity tuple for a current-state row."""
    return (row.component, row.subject_key_norm, row.current_key)


def _incident_identity(row: HealthIncidentModel) -> tuple[str, str, str]:
    """Return the active incident identity tuple for a row."""
    return (row.component, row.subject_key_norm, row.current_key)


def _subject_key_norm(subject_key: str | None) -> str:
    """Normalize nullable subject keys for portable uniqueness."""
    return subject_key or ""


def _details_to_json(details: dict[str, Any]) -> str | None:
    """Serialize non-empty health details for persistence."""
    return json.dumps(details) if details else None
