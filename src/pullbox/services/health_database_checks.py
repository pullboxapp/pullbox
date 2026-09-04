"""Database-specific health check implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.duration_format import format_duration_ms
from pullbox.models.health import HealthStatus
from pullbox.models.series import Series
from pullbox.services.health_helpers import (
    _STATUS_PRECEDENCE,
    _coerce_sub_check,
    _serialize_sub_check,
    _sqlite_database_path,
    _status_for_latency,
)
from pullbox.services.health_types import CheckOutcome, SubCheckOutcome

_DB_CONNECTION_DEGRADED_MS = 250.0
_DB_CONNECTION_UNHEALTHY_MS = 1000.0
_DB_QUERY_DEGRADED_MS = 500.0
_DB_QUERY_UNHEALTHY_MS = 1500.0
_DB_BLOAT_DEGRADED_RATIO = 0.15
_DB_BLOAT_UNHEALTHY_RATIO = 0.3
_DB_BLOAT_DEGRADED_MB = 50.0
_DB_BLOAT_UNHEALTHY_MB = 250.0

PerfCounter = Callable[[], float]
RequiredDatabaseCheck = Callable[[AsyncSession], Awaitable[SubCheckOutcome]]
OptionalDatabaseCheck = Callable[
    [AsyncSession], Awaitable[SubCheckOutcome | dict[str, object] | None]
]


async def check_database(
    session: AsyncSession,
    *,
    check_round_trip: RequiredDatabaseCheck,
    check_query_latency: RequiredDatabaseCheck,
    check_size: OptionalDatabaseCheck,
    check_integrity: OptionalDatabaseCheck,
    check_bloat: OptionalDatabaseCheck,
) -> CheckOutcome:
    """Verify database connectivity, realistic query performance, and SQLite health."""
    sub_checks: list[SubCheckOutcome] = []
    guidance_parts: list[str] = []

    round_trip_check = await check_round_trip(session)
    query_latency_check = await check_query_latency(session)
    sub_checks.extend([round_trip_check, query_latency_check])

    for raw_optional_check in (
        await check_size(session),
        await check_integrity(session),
        await check_bloat(session),
    ):
        optional_check = _coerce_sub_check(raw_optional_check)
        if optional_check is not None:
            sub_checks.append(optional_check)

    worst = max(
        (check.status for check in sub_checks),
        key=lambda status: _STATUS_PRECEDENCE.get(status, 0),
        default=HealthStatus.UNKNOWN,
    )

    for check in sub_checks:
        if check.check_name == "query_latency" and check.status != HealthStatus.HEALTHY:
            guidance_parts.append(
                "Series registry reads are slower than expected. Check disk I/O, recent vacuum "
                "activity, and whether other background jobs are contending for the database."
            )
        elif check.check_name == "database_size":
            if check.status == HealthStatus.DEGRADED:
                guidance_parts.append(
                    "Database size is growing. Consider running cleanup_history or "
                    "vacuuming the database."
                )
            elif check.status == HealthStatus.UNHEALTHY:
                guidance_parts.append(
                    "Database size is critically high. Run cleanup_history, prune old "
                    "audit data, and vacuum the database."
                )
        elif check.check_name == "database_bloat" and check.status != HealthStatus.HEALTHY:
            guidance_parts.append(
                "SQLite free-list bloat is high. Vacuuming the database should reclaim "
                "unused pages."
            )
        elif check.check_name == "integrity_check" and check.status != HealthStatus.HEALTHY:
            guidance_parts.append(
                "SQLite quick_check reported an integrity issue. Stop background work "
                "and inspect the database."
            )

    if any(
        check.check_name == "integrity_check" and check.status == HealthStatus.UNHEALTHY
        for check in sub_checks
    ):
        msg = "Integrity check failed"
    elif any(
        check.check_name in {"connection_round_trip", "query_latency"}
        and check.status != HealthStatus.HEALTHY
        for check in sub_checks
    ):
        msg = "Database performance degraded"
    elif any(
        check.check_name in {"database_size", "database_bloat"}
        and check.status != HealthStatus.HEALTHY
        for check in sub_checks
    ):
        msg = "Database storage issue"
    elif worst == HealthStatus.HEALTHY:
        msg = "Connected"
    else:
        msg = "Database issues detected"

    response_time_ms = (
        query_latency_check.response_time_ms or round_trip_check.response_time_ms or 0.0
    )
    return CheckOutcome(
        component="database",
        check_name="connectivity",
        status=worst,
        message=msg,
        details={
            "latency_ms": (
                f"{query_latency_check.response_time_ms:.1f}"
                if query_latency_check.response_time_ms is not None
                else None
            ),
            "checks": [_serialize_sub_check(check) for check in sub_checks],
        },
        response_time_ms=response_time_ms,
        actionable_guidance=" ".join(dict.fromkeys(guidance_parts)),
        sub_checks=tuple(sub_checks),
    )


async def check_database_round_trip(
    session: AsyncSession,
    *,
    perf_counter: PerfCounter,
) -> SubCheckOutcome:
    """Measure a lightweight connection round-trip for the current database."""
    start = perf_counter()
    await session.execute(text("SELECT 1"))
    elapsed_ms = (perf_counter() - start) * 1000
    status = _status_for_latency(
        elapsed_ms,
        degraded_ms=_DB_CONNECTION_DEGRADED_MS,
        unhealthy_ms=_DB_CONNECTION_UNHEALTHY_MS,
    )
    if status == HealthStatus.HEALTHY:
        message = f"SELECT 1 completed in {format_duration_ms(elapsed_ms)}"
    elif status == HealthStatus.DEGRADED:
        message = (
            "SELECT 1 took "
            f"{format_duration_ms(elapsed_ms)} "
            f"(threshold: {format_duration_ms(_DB_CONNECTION_DEGRADED_MS)})"
        )
    else:
        message = (
            "SELECT 1 took "
            f"{format_duration_ms(elapsed_ms)} "
            f"(threshold: {format_duration_ms(_DB_CONNECTION_UNHEALTHY_MS)})"
        )
    return SubCheckOutcome(
        check_name="connection_round_trip",
        name="Connection round trip",
        status=status,
        message=message,
        response_time_ms=elapsed_ms,
    )


async def check_database_query_latency(
    session: AsyncSession,
    *,
    perf_counter: PerfCounter,
) -> SubCheckOutcome:
    """Measure a representative indexed read against the series registry."""
    start = perf_counter()
    await session.execute(
        select(Series.id, Series.title, Series.issue_count)
        .order_by(Series.sort_title.asc())
        .limit(25)
    )
    elapsed_ms = (perf_counter() - start) * 1000
    status = _status_for_latency(
        elapsed_ms,
        degraded_ms=_DB_QUERY_DEGRADED_MS,
        unhealthy_ms=_DB_QUERY_UNHEALTHY_MS,
    )
    if status == HealthStatus.HEALTHY:
        message = f"Series registry read completed in {format_duration_ms(elapsed_ms)}"
    elif status == HealthStatus.DEGRADED:
        message = (
            "Series registry read took "
            f"{format_duration_ms(elapsed_ms)} "
            f"(threshold: {format_duration_ms(_DB_QUERY_DEGRADED_MS)})"
        )
    else:
        message = (
            "Series registry read took "
            f"{format_duration_ms(elapsed_ms)} "
            f"(threshold: {format_duration_ms(_DB_QUERY_UNHEALTHY_MS)})"
        )
    return SubCheckOutcome(
        check_name="query_latency",
        name="Query latency",
        status=status,
        message=message,
        response_time_ms=elapsed_ms,
    )


async def check_db_size(session: AsyncSession) -> SubCheckOutcome | None:
    """Check SQLite database file size."""
    db_path = _sqlite_database_path(session)
    if db_path is None:
        return None

    try:
        size_bytes = await asyncio.to_thread(lambda: db_path.stat().st_size)
    except OSError:
        return None

    size_mb = size_bytes / (1024 * 1024)

    return SubCheckOutcome(
        check_name="database_size",
        name="Database size",
        status=HealthStatus.HEALTHY,
        message=f"{size_mb:.1f} MB (informational)",
        details={
            "size_bytes": size_bytes,
            "classification": "informational",
        },
    )


async def check_db_integrity(session: AsyncSession) -> SubCheckOutcome | None:
    """Run SQLite quick_check when supported."""
    if _sqlite_database_path(session) is None:
        return None

    result = await session.execute(text("PRAGMA quick_check"))
    status_text = str(result.scalar_one_or_none() or "").strip()
    if not status_text:
        return None
    if status_text.lower() == "ok":
        return SubCheckOutcome(
            check_name="integrity_check",
            name="Integrity check",
            status=HealthStatus.HEALTHY,
            message="PRAGMA quick_check returned ok",
        )
    return SubCheckOutcome(
        check_name="integrity_check",
        name="Integrity check",
        status=HealthStatus.UNHEALTHY,
        message=f"PRAGMA quick_check reported {status_text}",
    )


async def check_db_bloat(session: AsyncSession) -> SubCheckOutcome | None:
    """Estimate SQLite bloat from free-list pages."""
    if _sqlite_database_path(session) is None:
        return None

    page_count_result = await session.execute(text("PRAGMA page_count"))
    freelist_result = await session.execute(text("PRAGMA freelist_count"))
    page_size_result = await session.execute(text("PRAGMA page_size"))

    page_count = int(page_count_result.scalar_one_or_none() or 0)
    freelist_count = int(freelist_result.scalar_one_or_none() or 0)
    page_size = int(page_size_result.scalar_one_or_none() or 0)
    if page_count <= 0 or page_size <= 0:
        return None

    reclaimable_bytes = freelist_count * page_size
    reclaimable_mb = reclaimable_bytes / (1024 * 1024)
    reclaimable_ratio = freelist_count / page_count if page_count else 0.0
    ratio_label = reclaimable_ratio * 100

    if reclaimable_ratio >= _DB_BLOAT_UNHEALTHY_RATIO and reclaimable_mb >= _DB_BLOAT_UNHEALTHY_MB:
        return SubCheckOutcome(
            check_name="database_bloat",
            name="Database bloat",
            status=HealthStatus.UNHEALTHY,
            message=f"{ratio_label:.0f}% reclaimable ({reclaimable_mb:.0f} MB free pages)",
            details={"reclaimable_mb": round(reclaimable_mb, 1)},
        )
    if reclaimable_ratio >= _DB_BLOAT_DEGRADED_RATIO and reclaimable_mb >= _DB_BLOAT_DEGRADED_MB:
        return SubCheckOutcome(
            check_name="database_bloat",
            name="Database bloat",
            status=HealthStatus.DEGRADED,
            message=f"{ratio_label:.0f}% reclaimable ({reclaimable_mb:.0f} MB free pages)",
            details={"reclaimable_mb": round(reclaimable_mb, 1)},
        )
    return SubCheckOutcome(
        check_name="database_bloat",
        name="Database bloat",
        status=HealthStatus.HEALTHY,
        message=f"{ratio_label:.0f}% reclaimable ({reclaimable_mb:.1f} MB free pages)",
        details={"reclaimable_mb": round(reclaimable_mb, 1)},
    )
