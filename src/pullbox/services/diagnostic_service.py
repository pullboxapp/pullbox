"""Diagnostic debug package — collects sanitized system info into a ZIP file."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from pullbox.services.diagnostic_db_snapshot import (
    create_sanitized_db_copy as _create_sanitized_db_copy,
)
from pullbox.services.diagnostic_log_collector import (
    MAX_LOG_FILE_BYTES as _MAX_LOG_FILE_BYTES,  # noqa: F401
)
from pullbox.services.diagnostic_log_collector import collect_log_files as _collect_log_files
from pullbox.services.diagnostic_package_writer import build_diagnostic_zip
from pullbox.services.diagnostic_runtime_collectors import (
    collect_bootstrap_settings as _collect_bootstrap_settings,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_config_xml_snapshot as _collect_config_xml_snapshot,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_container_runtime as _collect_container_runtime,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_installed_packages as _collect_installed_packages,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_runtime_info as _collect_runtime_info,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_scheduler_state as _collect_scheduler_state,
)
from pullbox.services.diagnostic_runtime_collectors import (
    collect_system_info as _collect_system_info,
)
from pullbox.services.diagnostic_sanitizer import coerce_json_safe as _coerce_json_safe
from pullbox.services.diagnostic_sanitizer import redact_value as _redact_value
from pullbox.services.diagnostic_storage_collectors import (
    collect_disk_and_permissions as _collect_disk_and_permissions,
)
from pullbox.services.diagnostic_utility_collectors import (
    collect_utility_job_logs as _collect_utility_job_logs,
)
from pullbox.services.diagnostic_utility_collectors import (
    collect_utility_jobs as _collect_utility_jobs,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


async def _collect_config(session: AsyncSession) -> list[dict[str, str]]:
    """Dump sanitized SystemConfig rows."""
    from sqlalchemy import select

    from pullbox.models.config import SystemConfig

    result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
    rows = result.scalars().all()
    return [
        {
            "key": row.key,
            "value": _redact_value(row.key, row.value),
            "value_type": row.value_type,
        }
        for row in rows
    ]


async def _collect_health_status(session: AsyncSession) -> list[dict[str, object]]:
    """Return the current health status per component."""
    from sqlalchemy import select

    from pullbox.models.health import HealthCurrentStatus

    stmt = select(HealthCurrentStatus).where(
        HealthCurrentStatus.is_summary.is_(True),
        HealthCurrentStatus.subject_key_norm == "",
    )
    result = await session.execute(stmt)
    checks = result.scalars().all()
    return [
        {
            "component": c.component,
            "check_name": c.check_name,
            "status": c.status,
            "message": c.message,
            "response_time_ms": c.response_time_ms,
            "checked_at": str(c.checked_at) if c.checked_at else None,
        }
        for c in checks
    ]


async def _collect_health_history(session: AsyncSession) -> list[dict[str, object]]:
    """Return recent health-check history rows for troubleshooting."""
    from sqlalchemy import select

    from pullbox.models.health import HealthCheckResult

    result = await session.execute(
        select(HealthCheckResult).order_by(HealthCheckResult.checked_at.desc()).limit(200)
    )
    checks = result.scalars().all()
    return [
        {
            "id": c.id,
            "component": c.component,
            "check_name": c.check_name,
            "subject_key": c.subject_key,
            "subject_label": c.subject_label,
            "status": str(c.status),
            "message": _coerce_json_safe(c.message, key="message"),
            "details_json": _coerce_json_safe(c.details_json, key="details_json"),
            "response_time_ms": c.response_time_ms,
            "run_id": c.run_id,
            "is_summary": c.is_summary,
            "checked_at": str(c.checked_at) if c.checked_at else None,
        }
        for c in checks
    ]


async def _collect_health_incidents(session: AsyncSession) -> list[dict[str, object]]:
    """Return compact health incident rows for troubleshooting."""
    from sqlalchemy import select

    from pullbox.models.health import HealthIncident

    result = await session.execute(
        select(HealthIncident).order_by(HealthIncident.last_seen_at.desc()).limit(200)
    )
    incidents = result.scalars().all()
    return [
        {
            "id": incident.id,
            "component": incident.component,
            "check_name": incident.check_name,
            "subject_key": incident.subject_key,
            "subject_label": incident.subject_label,
            "status": str(incident.status),
            "is_summary": incident.is_summary,
            "first_seen_at": str(incident.first_seen_at),
            "last_seen_at": str(incident.last_seen_at),
            "resolved_at": str(incident.resolved_at) if incident.resolved_at else None,
            "occurrence_count": incident.occurrence_count,
            "last_message": _coerce_json_safe(incident.last_message, key="last_message"),
            "last_details_json": _coerce_json_safe(
                incident.last_details_json,
                key="last_details_json",
            ),
            "last_response_time_ms": incident.last_response_time_ms,
            "last_run_id": incident.last_run_id,
        }
        for incident in incidents
    ]


async def _collect_database_stats(session: AsyncSession) -> dict[str, object]:
    """Collect row counts and database file size."""
    from sqlalchemy import func, select, text

    from pullbox.config import get_settings
    from pullbox.models.config import SystemConfig
    from pullbox.models.download import DownloadHistory
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFile
    from pullbox.models.series import Series

    tables = {
        "series": select(func.count()).select_from(Series),
        "issues": select(func.count()).select_from(Issue),
        "downloads": select(func.count()).select_from(DownloadHistory),
        "library_files": select(func.count()).select_from(LibraryFile),
        "config": select(func.count()).select_from(SystemConfig),
    }

    counts: dict[str, int] = {}
    for name, stmt in tables.items():
        try:
            result = await session.execute(stmt)
            counts[name] = result.scalar() or 0
        except Exception:
            counts[name] = -1

    # Database file size
    db_size: int | None = None
    try:
        settings = get_settings()
        db_url = settings.db_url
        if ":///" in db_url:
            db_path = Path(db_url.split(":///", 1)[1])
            if db_path.exists():
                db_size = db_path.stat().st_size
    except Exception:
        pass

    # SQLite version
    sqlite_version: str | None = None
    try:
        result = await session.execute(text("SELECT sqlite_version()"))
        raw = result.scalar()
        sqlite_version = str(raw) if raw is not None else None
    except Exception:
        pass

    return {
        "row_counts": counts,
        "db_file_size_bytes": db_size,
        "sqlite_version": sqlite_version,
    }


async def _collect_sqlite_runtime(session: AsyncSession) -> dict[str, object]:
    """Collect active SQLite runtime configuration and snapshot health."""
    from sqlalchemy import text

    from pullbox.config import get_settings

    settings = get_settings()
    diagnostics: dict[str, object] = {
        "db_url": _coerce_json_safe(settings.db_url, key="db_url"),
        "db_path": settings.db_url.split(":///", 1)[1] if ":///" in settings.db_url else None,
    }
    pragma_queries = {
        "journal_mode": text("PRAGMA journal_mode;"),
        "synchronous": text("PRAGMA synchronous;"),
        "busy_timeout": text("PRAGMA busy_timeout;"),
        "foreign_keys": text("PRAGMA foreign_keys;"),
        "quick_check": text("PRAGMA quick_check;"),
    }
    for key, stmt in pragma_queries.items():
        try:
            result = await session.execute(stmt)
            diagnostics[key] = _coerce_json_safe(result.scalar_one_or_none(), key=key)
        except Exception as exc:
            diagnostics[f"{key}_error"] = _coerce_json_safe(str(exc), key=f"{key}_error")

    if diagnostics["db_path"]:
        db_path = Path(str(diagnostics["db_path"]))
        wal_path = db_path.with_name(f"{db_path.name}-wal")
        shm_path = db_path.with_name(f"{db_path.name}-shm")
        diagnostics["wal_exists"] = wal_path.exists()
        diagnostics["shm_exists"] = shm_path.exists()
        diagnostics["wal_size_bytes"] = wal_path.stat().st_size if wal_path.exists() else None
        diagnostics["shm_size_bytes"] = shm_path.stat().st_size if shm_path.exists() else None

    return diagnostics


async def _collect_download_history(session: AsyncSession) -> list[dict[str, object]]:
    """Return last 100 download history entries."""
    from sqlalchemy import select

    from pullbox.models.download import DownloadClientType, DownloadHistory

    result = await session.execute(
        select(DownloadHistory).order_by(DownloadHistory.created_at.desc()).limit(100)
    )
    downloads = result.scalars().all()
    return [
        {
            "id": d.id,
            "issue_id": d.issue_id,
            "title": d.title,
            "state": str(d.state),
            "download_client": str(d.download_client),
            "file_size": d.file_size,
            "downloaded_path": (
                "[REDACTED]"
                if d.download_client is DownloadClientType.AIRDCPP and d.downloaded_path
                else d.downloaded_path
            ),
            "final_path": d.final_path,
            "error_message": d.error_message,
            "retry_count": d.retry_count,
            "created_at": str(d.created_at) if d.created_at else None,
            "completed_at": str(d.completed_at) if d.completed_at else None,
            "imported_at": str(d.imported_at) if d.imported_at else None,
        }
        for d in downloads
    ]


async def _collect_search_logs(session: AsyncSession) -> list[dict[str, object]]:
    """Return last 100 search log entries."""
    from sqlalchemy import select

    from pullbox.models.search_log import SearchLog

    result = await session.execute(
        select(SearchLog).order_by(SearchLog.created_at.desc()).limit(100)
    )
    logs = result.scalars().all()
    return [
        {
            "id": s.id,
            "issue_id": s.issue_id,
            "series_title": s.series_title,
            "issue_number": s.issue_number,
            "search_type": str(s.search_type),
            "results_found": s.results_found,
            "results_grabbed": s.results_grabbed,
            "results_queued": s.results_queued,
            "results_rejected": s.results_rejected,
            "best_confidence": s.best_confidence,
            "details": s.details,
            "created_at": str(s.created_at) if s.created_at else None,
        }
        for s in logs
    ]


async def _collect_pending_matches(session: AsyncSession) -> list[dict[str, object]]:
    """Return last 200 intervention queue entries (all statuses)."""
    from sqlalchemy import select

    from pullbox.models.pending_match import PendingMatch

    result = await session.execute(
        select(PendingMatch).order_by(PendingMatch.created_at.desc()).limit(200)
    )
    matches = result.scalars().all()
    return [
        {
            "id": m.id,
            "issue_id": m.issue_id,
            "release_title": m.release_title,
            "confidence": m.confidence,
            "status": m.status,
            "is_torrent": m.is_torrent,
            "file_size": m.file_size,
            "match_details": m.match_details,
            "resolved_at": str(m.resolved_at) if m.resolved_at else None,
            "resolved_by": m.resolved_by,
            "created_at": str(m.created_at) if m.created_at else None,
        }
        for m in matches
    ]


async def _collect_import_jobs(session: AsyncSession) -> list[dict[str, object]]:
    """Return last 20 import jobs with status, counters, and timestamps."""
    from sqlalchemy import select

    from pullbox.models.import_job import ImportJob

    result = await session.execute(
        select(ImportJob).order_by(ImportJob.created_at.desc()).limit(20)
    )
    jobs = result.scalars().all()
    return [
        {
            "id": j.id,
            "source_type": str(j.source_type),
            "source_path": j.source_path,
            "status": str(j.status),
            "series_found": j.series_found,
            "series_matched": j.series_matched,
            "series_imported": j.series_imported,
            "series_failed": j.series_failed,
            "total_files_found": j.total_files_found,
            "total_files_matched": j.total_files_matched,
            "total_files_imported": j.total_files_imported,
            "total_files_failed": j.total_files_failed,
            "error_message": j.error_message,
            "scan_started_at": str(j.scan_started_at) if j.scan_started_at else None,
            "scan_completed_at": str(j.scan_completed_at) if j.scan_completed_at else None,
            "match_started_at": str(j.match_started_at) if j.match_started_at else None,
            "match_completed_at": str(j.match_completed_at) if j.match_completed_at else None,
            "import_started_at": str(j.import_started_at) if j.import_started_at else None,
            "import_completed_at": str(j.import_completed_at) if j.import_completed_at else None,
            "created_at": str(j.created_at) if j.created_at else None,
        }
        for j in jobs
    ]


async def create_diagnostic_package(session: AsyncSession) -> tuple[bytes, str]:
    """Create a diagnostic debug ZIP package.

    Returns:
        Tuple of (zip_bytes, filename).
    """
    from pullbox.config import get_settings

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = f"pullbox-diagnostic-{timestamp}"
    filename = f"{prefix}.zip"

    # Collect all data
    system_info = await _collect_system_info()
    bootstrap_settings = await _collect_bootstrap_settings()
    container_runtime = _collect_container_runtime()
    config = await _collect_config(session)
    config_xml_snapshot = _collect_config_xml_snapshot()
    health = await _collect_health_status(session)
    health_history = await _collect_health_history(session)
    health_incidents = await _collect_health_incidents(session)
    db_stats = await _collect_database_stats(session)
    sqlite_runtime = await _collect_sqlite_runtime(session)
    downloads = await _collect_download_history(session)
    search_logs = await _collect_search_logs(session)
    pending_matches = await _collect_pending_matches(session)
    packages = await _collect_installed_packages()
    scheduler_state = _collect_scheduler_state()
    disk_permissions = await _collect_disk_and_permissions(session)
    runtime_info = await _collect_runtime_info(session)
    import_jobs = await _collect_import_jobs(session)
    utility_jobs = await _collect_utility_jobs(session)
    utility_job_logs = await _collect_utility_job_logs(
        session,
        [str(job["id"]) for job in utility_jobs],
    )

    # Get logs directory from runtime settings
    log_files = _collect_log_files(get_settings().logs_dir)

    # Create sanitized database copy
    db_copy: bytes | None = None
    try:
        settings = get_settings()
        if ":///" in settings.db_url:
            db_path = Path(settings.db_url.split(":///", 1)[1])
            db_copy = _create_sanitized_db_copy(db_path)
    except Exception:
        logger.warning("diagnostic_db_snapshot_skipped", exc_info=True)

    binary_artifacts = [config_xml_snapshot] if config_xml_snapshot is not None else []
    zip_bytes = build_diagnostic_zip(
        prefix=prefix,
        json_artifacts={
            "system_info.json": system_info,
            "bootstrap_settings.json": bootstrap_settings,
            "container_runtime.json": container_runtime,
            "config.json": config,
            "health_status.json": health,
            "health_history.json": health_history,
            "health_incidents.json": health_incidents,
            "database_stats.json": db_stats,
            "sqlite_runtime.json": sqlite_runtime,
            "download_history.json": downloads,
            "search_logs.json": search_logs,
            "pending_matches.json": pending_matches,
            "installed_packages.json": packages,
            "scheduler_state.json": scheduler_state,
            "disk_and_permissions.json": disk_permissions,
            "runtime_info.json": runtime_info,
            "import_jobs.json": import_jobs,
            "utility_jobs.json": utility_jobs,
            "utility_job_logs.json": utility_job_logs,
        },
        binary_artifacts=binary_artifacts,
        log_files=log_files,
        db_copy=db_copy,
    )
    logger.info(
        "diagnostic_package_created",
        filename=filename,
        size_bytes=len(zip_bytes),
        log_files_count=len(log_files),
    )
    return zip_bytes, filename
