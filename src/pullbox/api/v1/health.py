"""Health and system status API routes.

Provides authenticated health endpoints with per-component detail,
history retrieval, and on-demand refresh.  The unauthenticated /ping
endpoint lives in app.py.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Query
from sqlalchemy import delete, func, select
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

import pullbox
from pullbox.api.deps import DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.core.exceptions import ValidationError
from pullbox.database import DatabaseMaintenanceBusyError
from pullbox.models.health import HealthCurrentStatus as HealthCurrentStatusModel
from pullbox.models.health import HealthStatus
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.schemas.health import (
    HealthCheckResponse,
    HealthComponentDetail,
    HealthComponentResponse,
    HealthHistoryItem,
    HealthHistoryResponse,
    HealthIncidentItem,
    HealthIncidentResponse,
    HealthSummary,
)
from pullbox.services.database_optimization_service import (
    DatabaseOptimizationError,
    DatabaseOptimizationResult,
    DatabaseOptimizationRuntimeService,
)
from pullbox.services.health_helpers import _sqlite_database_path
from pullbox.services.health_runtime import run_health_refresh
from pullbox.services.health_service import HealthService
from pullbox.utilities.models import JobState, UtilityJob

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"], include_in_schema=False)

_started_at: datetime = datetime.now(UTC)

# Components that the health engine knows about
_KNOWN_COMPONENTS = frozenset(
    {"database", "filesystem", "comicvine", "download_clients", "indexers", "scheduler", "system"}
)
_COMPONENT_ORDER = (
    "database",
    "filesystem",
    "comicvine",
    "download_clients",
    "indexers",
    "scheduler",
    "system",
)

_DATABASE_OPTIMIZE_BLOCKING_IMPORT_STATUSES = frozenset(
    {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }
)
_DATABASE_OPTIMIZE_BLOCKING_UTILITY_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.PAUSING,
        JobState.PAUSED,
        JobState.CANCELLING,
        JobState.ROLLING_BACK,
    }
)


def _outcome_subject_key(outcome: object) -> str | None:
    """Return a normalized subject key from a health outcome-like object."""
    value = getattr(outcome, "subject_key", None)
    return value if isinstance(value, str) and value else None


async def _ensure_database_optimization_is_admissible(session: DbSession) -> None:
    """Reject compaction while stateful import or utility work is active."""
    active_import = await session.scalar(
        select(ImportJob.id)
        .where(ImportJob.status.in_(_DATABASE_OPTIMIZE_BLOCKING_IMPORT_STATUSES))
        .limit(1)
    )
    if active_import is not None:
        raise ValidationError(
            "Database optimization is unavailable while an import or rollback is active."
        )

    active_utility = await session.scalar(
        select(UtilityJob.id)
        .where(UtilityJob.state.in_(_DATABASE_OPTIMIZE_BLOCKING_UTILITY_STATES))
        .limit(1)
    )
    if active_utility is not None:
        raise ValidationError(
            "Database optimization is unavailable while a utility job is queued or running."
        )


def _database_optimization_response_payload(
    result: DatabaseOptimizationResult,
) -> dict[str, object]:
    """Build the stable API result shape without exposing filesystem paths."""
    before = result.before
    after = result.after
    return {
        "message": "Database optimization completed.",
        "reclaimed_bytes": int(result.reclaimed_bytes),
        "before": {
            "database_bytes": int(before.database_bytes),
            "wal_bytes": int(before.wal_bytes),
            "free_pages": int(before.free_pages),
            "reclaimable_bytes": int(before.reclaimable_bytes),
        },
        "after": {
            "database_bytes": int(after.database_bytes),
            "wal_bytes": int(after.wal_bytes),
            "free_pages": int(after.free_pages),
            "reclaimable_bytes": int(after.reclaimable_bytes),
        },
    }


# ── Health ───────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthCheckResponse)
async def health_overview(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JSONResponse:
    """Full system health report with per-component detail.

    Returns HTTP 200 when healthy or degraded, HTTP 503 when unhealthy.
    """
    rows = await _load_latest_component_summary_rows(session)
    components = [_summary_row_to_component_detail(row) for row in rows]

    # Summary counts
    summary = HealthSummary(
        healthy=sum(1 for row in rows if row.status == HealthStatus.HEALTHY),
        degraded=sum(1 for row in rows if row.status == HealthStatus.DEGRADED),
        unhealthy=sum(1 for row in rows if row.status == HealthStatus.UNHEALTHY),
        unknown=sum(1 for row in rows if row.status == HealthStatus.UNKNOWN),
        total_check_time_ms=round(
            sum(float(row.response_time_ms or 0.0) for row in rows),
            1,
        ),
    )

    overall = await HealthService.get_overall_status(session)
    timestamp = max((row.checked_at for row in rows), default=datetime.now(UTC))

    response = HealthCheckResponse(
        status=overall,
        timestamp=timestamp,
        components=components,
        summary=summary,
    )

    status_code = 503 if overall == HealthStatus.UNHEALTHY else 200
    headers = {
        "X-Health-Status": overall.value,
        "X-Health-Check-Duration-Ms": str(summary.total_check_time_ms),
    }
    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=status_code,
        headers=headers,
    )


@router.get("/health/incidents", response_model=HealthIncidentResponse)
async def health_incidents(
    _user: InteractiveOperatorUser,
    session: DbSession,
    component: str | None = Query(None),
    limit: int = 50,
    include_resolved: bool = True,
) -> JSONResponse:
    """Retrieve compact long-term non-healthy health spans."""
    if component is not None and component not in _KNOWN_COMPONENTS:
        return JSONResponse(
            content={"detail": f"Unknown component: {component}"},
            status_code=404,
        )

    rows = await HealthService.get_incidents(
        session,
        component=component,
        limit=limit,
        include_resolved=include_resolved,
    )
    items = [
        HealthIncidentItem(
            id=row.id,
            component=row.component,
            check_name=row.check_name,
            subject_key=row.subject_key,
            subject_label=row.subject_label,
            status=row.status,
            message=row.last_message,
            details=_parse_health_details_json(row.last_details_json),
            response_time_ms=row.last_response_time_ms,
            occurrence_count=row.occurrence_count,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]
    response = HealthIncidentResponse(items=items, total=len(items))
    return JSONResponse(content=response.model_dump(mode="json"), status_code=200)


@router.get("/health/{component}", response_model=HealthComponentResponse)
async def health_component(
    component: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JSONResponse:
    """Health detail for a single component."""
    if component not in _KNOWN_COMPONENTS:
        return JSONResponse(
            content={"detail": f"Unknown component: {component}"},
            status_code=404,
        )

    row = await _load_latest_component_summary_row(session, component)
    if row is None:
        response = HealthComponentResponse(
            component=component,
            status=HealthStatus.UNKNOWN,
            checks=[],
        )
        return JSONResponse(content=response.model_dump(mode="json"), status_code=200)

    checks = _component_checks_from_summary_row(row)

    response = HealthComponentResponse(
        component=component,
        status=row.status,
        checks=checks,
    )
    return JSONResponse(content=response.model_dump(mode="json"), status_code=200)


@router.get("/health/{component}/history", response_model=HealthHistoryResponse)
async def health_component_history(
    component: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    limit: int = 50,
    subject_key: str | None = Query(None),
) -> JSONResponse:
    """Retrieve check history for a component."""
    if component not in _KNOWN_COMPONENTS:
        return JSONResponse(
            content={"detail": f"Unknown component: {component}"},
            status_code=404,
        )

    rows = await HealthService.get_history(
        session,
        component=component,
        limit=limit,
        is_summary=not await _component_history_uses_subchecks(session, component, subject_key),
        subject_key=subject_key,
    )

    items = []
    for row in rows:
        details = None
        if row.details_json:
            try:
                details = json.loads(row.details_json)
            except (json.JSONDecodeError, TypeError):
                details = None

        items.append(
            HealthHistoryItem(
                id=row.id,
                component=row.component,
                check_name=row.check_name,
                subject_key=row.subject_key,
                subject_label=row.subject_label,
                status=row.status,
                message=row.message,
                details=details,
                response_time_ms=row.response_time_ms,
                checked_at=row.checked_at,
            )
        )

    response = HealthHistoryResponse(items=items, total=len(items))
    return JSONResponse(content=response.model_dump(mode="json"), status_code=200)


@router.delete("/health/{component}/history")
async def clear_health_component_history(
    component: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    subject_key: str | None = Query(None),
) -> JSONResponse:
    """Delete all stored health history rows for a component."""
    from pullbox.models.health import HealthCheckResult

    if component not in _KNOWN_COMPONENTS:
        return JSONResponse(
            content={"detail": f"Unknown component: {component}"},
            status_code=404,
        )

    deleted = int(
        (
            await session.execute(
                select(func.count(HealthCheckResult.id)).where(
                    HealthCheckResult.component == component,
                    HealthCheckResult.subject_key.is_(None)
                    if subject_key is None
                    else HealthCheckResult.subject_key == subject_key,
                )
            )
        ).scalar_one()
        or 0
    )

    if deleted:
        await session.execute(
            delete(HealthCheckResult).where(
                HealthCheckResult.component == component,
                HealthCheckResult.subject_key.is_(None)
                if subject_key is None
                else HealthCheckResult.subject_key == subject_key,
            )
        )
        await session.flush()

    logger.info("health_component_history_cleared", component=component, deleted=deleted)
    return JSONResponse(content={"deleted": deleted}, status_code=200)


@router.post("/health/refresh")
async def health_refresh(
    _user: InteractiveOperatorUser,
) -> JSONResponse:
    """Trigger an on-demand health check run."""
    outcomes = await run_health_refresh()
    top_level_outcomes = [o for o in outcomes if _outcome_subject_key(o) is None]

    summary = {
        "healthy": sum(1 for o in top_level_outcomes if o.status == HealthStatus.HEALTHY),
        "degraded": sum(1 for o in top_level_outcomes if o.status == HealthStatus.DEGRADED),
        "unhealthy": sum(1 for o in top_level_outcomes if o.status == HealthStatus.UNHEALTHY),
        "total_checks": len(top_level_outcomes),
    }
    logger.info("health_refresh_triggered", **summary)
    return JSONResponse(content={"message": "Health checks completed", **summary}, status_code=200)


@router.post("/health/{component}/refresh")
async def health_component_refresh(
    component: str,
    _user: InteractiveOperatorUser,
) -> JSONResponse:
    """Trigger an on-demand refresh for a single health component."""
    if component not in _KNOWN_COMPONENTS:
        return JSONResponse(
            content={"detail": f"Unknown component: {component}"},
            status_code=404,
        )

    outcomes = await run_health_refresh(component=component)
    summary = {
        "healthy": sum(1 for o in outcomes if o.status == HealthStatus.HEALTHY),
        "degraded": sum(1 for o in outcomes if o.status == HealthStatus.DEGRADED),
        "unhealthy": sum(1 for o in outcomes if o.status == HealthStatus.UNHEALTHY),
        "total_checks": len(outcomes),
    }
    logger.info("health_component_refresh_triggered", component=component, **summary)
    return JSONResponse(
        content={"message": "Health component check completed", "component": component, **summary},
        status_code=200,
    )


@router.post("/health/database/optimize")
async def optimize_database(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JSONResponse:
    """Compact SQLite free-list pages after explicit UI confirmation."""
    db_path = _sqlite_database_path(session)
    if db_path is None:
        raise ValidationError(
            "Database optimization is only available for file-backed SQLite databases."
        )

    await _ensure_database_optimization_is_admissible(session)
    # Release the request transaction before the exclusive maintenance window starts.
    await session.commit()
    await session.close()

    try:
        result = await DatabaseOptimizationRuntimeService(db_path).optimize()
    except (DatabaseOptimizationError, DatabaseMaintenanceBusyError) as exc:
        raise ValidationError(str(exc)) from exc

    await run_health_refresh(component="database")
    payload = _database_optimization_response_payload(result)
    logger.info(
        "database_optimized",
        reclaimed_bytes=result.reclaimed_bytes,
        before_bytes=result.before.database_bytes,
        after_bytes=result.after.database_bytes,
    )
    return JSONResponse(content=payload, status_code=200)


# ── System Status ────────────────────────────────────────────────────


@router.get("/system/status")
async def system_status(
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Detailed system information."""
    now = datetime.now(UTC)
    uptime_seconds = (now - _started_at).total_seconds()

    return {
        "version": pullbox.__version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "uptime_seconds": round(uptime_seconds, 1),
        "started_at": _started_at.isoformat(),
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_health_details_json(details_json: str | None) -> dict[str, Any] | None:
    """Safely deserialize persisted health details."""
    if not details_json:
        return None
    try:
        parsed = json.loads(details_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_health_status(value: object) -> HealthStatus:
    """Normalize persisted status strings into the enum."""
    if isinstance(value, HealthStatus):
        return value
    try:
        return HealthStatus(str(value))
    except ValueError:
        return HealthStatus.UNKNOWN


def _summary_row_to_component_detail(row: HealthCurrentStatusModel) -> HealthComponentDetail:
    """Convert a persisted component summary row into the overview schema."""
    return HealthComponentDetail(
        component=row.component,
        status=row.status,
        message=row.message,
        details=_parse_health_details_json(row.details_json),
        response_time_ms=round(float(row.response_time_ms or 0.0), 1),
        last_checked=row.checked_at,
        actionable_guidance=None,
    )


def _component_checks_from_summary_row(
    row: HealthCurrentStatusModel,
) -> list[HealthComponentDetail]:
    """Return component check details from a persisted summary row."""
    details = _parse_health_details_json(row.details_json)
    raw_checks = details.get("checks") if isinstance(details, Mapping) else None
    if isinstance(raw_checks, Sequence):
        checks: list[HealthComponentDetail] = []
        for raw_check in raw_checks:
            if not isinstance(raw_check, Mapping):
                continue
            check_details = dict(raw_check)
            checks.append(
                HealthComponentDetail(
                    component=row.component,
                    status=_coerce_health_status(raw_check.get("status")),
                    message=(
                        str(raw_check["message"]) if raw_check.get("message") is not None else None
                    ),
                    details=check_details,
                    response_time_ms=(
                        None
                        if raw_check.get("response_time_ms") is None
                        else round(float(raw_check["response_time_ms"]), 1)
                    ),
                    last_checked=row.checked_at,
                    actionable_guidance=(
                        str(raw_check["actionable_guidance"])
                        if raw_check.get("actionable_guidance") is not None
                        else None
                    ),
                )
            )
        if checks:
            return checks

    return [_summary_row_to_component_detail(row)]


async def _load_latest_component_summary_rows(
    session: AsyncSession,
) -> list[HealthCurrentStatusModel]:
    """Load current top-level summary rows for each component."""
    rows = (
        (
            await session.execute(
                select(HealthCurrentStatusModel).where(
                    HealthCurrentStatusModel.is_summary.is_(True),
                    HealthCurrentStatusModel.subject_key_norm == "",
                )
            )
        )
        .scalars()
        .all()
    )
    order = {name: index for index, name in enumerate(_COMPONENT_ORDER)}
    return sorted(rows, key=lambda row: (order.get(row.component, len(order)), row.component))


async def _load_latest_component_summary_row(
    session: AsyncSession,
    component: str,
) -> HealthCurrentStatusModel | None:
    """Load the current top-level summary row for one component."""
    result = await session.execute(
        select(HealthCurrentStatusModel)
        .where(
            HealthCurrentStatusModel.component == component,
            HealthCurrentStatusModel.is_summary.is_(True),
            HealthCurrentStatusModel.subject_key_norm == "",
        )
        .limit(1)
    )
    return result.scalars().first()


async def _component_history_uses_subchecks(
    session: AsyncSession,
    component: str,
    subject_key: str | None = None,
) -> bool:
    """Return True when a component has dedicated sub-check history rows."""
    from pullbox.models.health import HealthCheckResult

    count = int(
        (
            await session.execute(
                select(func.count(HealthCheckResult.id)).where(
                    HealthCheckResult.component == component,
                    HealthCheckResult.is_summary.is_(False),
                    HealthCheckResult.subject_key.is_(None)
                    if subject_key is None
                    else HealthCheckResult.subject_key == subject_key,
                )
            )
        ).scalar_one()
        or 0
    )
    return count > 0
