"""System API routes — backup management, log files, and system operations."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.api.v1.system_debug_logging import (
    DebugLoggingRequest,
    DebugLoggingStatusResponse,
    check_and_clear_expired_debug_logging_override,
    disable_debug_logging_response,
    enable_debug_logging_response,
    get_debug_logging_status_response,
)
from pullbox.api.v1.system_log_routes import (
    clear_log_files,
    delete_log_file,
    download_log_file,
    list_log_files,
    stream_log_file,
    view_log_file,
)
from pullbox.api.v1.system_log_routes import (
    router as system_log_router,
)
from pullbox.api.v1.system_logs import (
    LogContentResponse,
    LogFileResponse,
)
from pullbox.api.v1.system_logs import (
    is_valid_log_path as _is_valid_log_path,
)
from pullbox.api.v1.system_logs import (
    matches_level as _matches_level,
)
from pullbox.api.v1.system_logs import (
    validate_safe_filename as _validate_safe_filename,
)
from pullbox.config import get_settings
from pullbox.core.build_metadata import get_build_metadata
from pullbox.core.config_resolver import load_system_config_values
from pullbox.core.shutdown import shutdown_manager
from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
from pullbox.services.backup_runtime_service import BackupRuntimeService
from pullbox.services.backup_service import BackupService
from pullbox.services.usage_stats_telemetry import queue_usage_stats_ping

__all__ = [
    "LogContentResponse",
    "LogFileResponse",
    "_is_valid_log_path",
    "_matches_level",
    "_validate_safe_filename",
    "clear_log_files",
    "delete_log_file",
    "download_log_file",
    "list_log_files",
    "stream_log_file",
    "view_log_file",
]

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/system", tags=["system"], include_in_schema=False)
_USAGE_STATS_CONSENT_STATES = frozenset({"unknown", "enabled", "disabled"})


# ── Schemas ──────────────────────────────────────────────────────────


class BackupResponse(BaseModel):
    """Details about a backup archive."""

    filename: str
    created_at: str
    size_bytes: int
    pullbox_version: str
    db_size_bytes: int
    backup_type: str


class BackupCreatedResponse(BaseModel):
    """Response after creating a backup."""

    message: str = Field(description="Success message")
    backup: BackupResponse


class RestoreResponse(BaseModel):
    """Response after restoring from a backup."""

    message: str = Field(description="Status message")
    restart_required: bool = True


class UsageStatsPreferenceResponse(BaseModel):
    """Install-level anonymous usage stats preference."""

    consent: Literal["unknown", "enabled", "disabled"]
    enabled: bool
    prompt_pending: bool


class UsageStatsPreferenceUpdate(BaseModel):
    """Request body for updating the anonymous usage stats preference."""

    enabled: bool


# ── Helpers ──────────────────────────────────────────────────────────


def _resolve_db_path() -> Path:
    """Extract the SQLite file path from the configured database URL."""
    from pullbox.config import get_settings

    settings = get_settings()
    db_url = settings.db_url
    if ":///" in db_url:
        raw_path = db_url.split(":///", 1)[1]
    elif "://" in db_url:
        raw_path = db_url.split("://", 1)[1]
    else:
        raw_path = db_url
    return Path(raw_path)


async def _get_backup_service(session: DbSession) -> BackupService:
    """Build a BackupService from runtime path + DB-backed policy."""
    backup_dir = get_settings().backup_dir
    db_path = _resolve_db_path()
    return BackupService(backup_dir=backup_dir, db_path=db_path)


async def _get_backup_runtime_service(session: DbSession) -> BackupRuntimeService:
    """Build a runtime-safe backup orchestrator from runtime paths."""
    backup_dir = get_settings().backup_dir
    db_path = _resolve_db_path()
    return BackupRuntimeService(backup_dir=backup_dir, db_path=db_path)


def _normalize_usage_stats_consent(value: str | None) -> Literal["unknown", "enabled", "disabled"]:
    """Normalize stored usage-stats consent state."""
    normalized = (value or "unknown").strip().lower()
    if normalized not in _USAGE_STATS_CONSENT_STATES:
        return "unknown"
    return normalized  # type: ignore[return-value]


async def _read_usage_stats_preference(session: DbSession) -> UsageStatsPreferenceResponse:
    """Load the effective install-level usage-stats preference."""
    row = await session.get(SystemConfig, "usage_stats_consent")
    consent = _normalize_usage_stats_consent(row.value if row is not None else None)
    return UsageStatsPreferenceResponse(
        consent=consent,
        enabled=consent == "enabled",
        prompt_pending=consent == "unknown",
    )


async def _ensure_usage_stats_instance_id(session: DbSession) -> str:
    """Create the anonymous telemetry instance ID once consent is enabled."""
    config = await session.get(SystemConfig, "usage_stats_instance_id")
    if config is not None and config.value.strip():
        return config.value

    if config is None:
        default_value, default_type = DEFAULT_SYSTEM_CONFIG["usage_stats_instance_id"]
        config = SystemConfig(
            key="usage_stats_instance_id",
            value=default_value,
            value_type=default_type,
        )
        session.add(config)

    config.value = str(uuid4())
    config.value_type = "string"
    await session.flush()
    return config.value


def _session_factory_from_request(request: Request) -> Any:
    return getattr(request.app.state, "db_session_factory", None)


class ComicsDirectoryRequest(BaseModel):
    """Request to set the primary comics directory."""

    path: str = Field(..., min_length=1, description="Absolute path to the comics directory")


class ComicsDirectoryResponse(BaseModel):
    """Response after setting the comics directory."""

    path: str
    library_root_id: int


# ── Routes ───────────────────────────────────────────────────────────


@router.get("/usage-stats", response_model=UsageStatsPreferenceResponse)
async def get_usage_stats_preference(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> UsageStatsPreferenceResponse:
    """Return the install-level anonymous usage stats preference."""
    return await _read_usage_stats_preference(session)


@router.put("/usage-stats", response_model=UsageStatsPreferenceResponse)
async def update_usage_stats_preference(
    body: UsageStatsPreferenceUpdate,
    request: Request,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> UsageStatsPreferenceResponse:
    """Update the install-level anonymous usage stats preference."""
    next_consent = "enabled" if body.enabled else "disabled"
    config = await session.get(SystemConfig, "usage_stats_consent")
    previous_consent = _normalize_usage_stats_consent(config.value if config is not None else None)

    if config is None:
        default_value, default_type = DEFAULT_SYSTEM_CONFIG["usage_stats_consent"]
        config = SystemConfig(
            key="usage_stats_consent",
            value=default_value,
            value_type=default_type,
        )
        session.add(config)

    config.value = next_consent
    await session.flush()

    if previous_consent != next_consent:
        logger.info(
            "usage_stats_preference_updated",
            previous_consent=previous_consent,
            consent=next_consent,
            enabled=body.enabled,
            store="database",
        )

    if next_consent == "enabled":
        await _ensure_usage_stats_instance_id(session)

    if previous_consent != next_consent and next_consent == "enabled":
        await session.commit()
        await queue_usage_stats_ping(session_factory=_session_factory_from_request(request))

    return await _read_usage_stats_preference(session)


@router.post("/backup", response_model=BackupCreatedResponse)
async def create_backup(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> BackupCreatedResponse:
    """Create a manual backup of the Pullbox database."""
    svc = await _get_backup_runtime_service(session)
    info = await svc.create_backup(backup_type="manual")
    return BackupCreatedResponse(
        message=f"Backup created: {info.filename}",
        backup=BackupResponse(
            filename=info.filename,
            created_at=info.created_at,
            size_bytes=info.size_bytes,
            pullbox_version=info.pullbox_version,
            db_size_bytes=info.db_size_bytes,
            backup_type=info.backup_type,
        ),
    )


@router.get("/backups", response_model=list[BackupResponse])
async def list_backups(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> list[BackupResponse]:
    """List all existing backup archives."""
    svc = await _get_backup_service(session)
    backups = svc.list_backups()
    return [
        BackupResponse(
            filename=b.filename,
            created_at=b.created_at,
            size_bytes=b.size_bytes,
            pullbox_version=b.pullbox_version,
            db_size_bytes=b.db_size_bytes,
            backup_type=b.backup_type,
        )
        for b in backups
    ]


@router.get("/backups/{filename}")
async def download_backup(
    filename: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> FileResponse:
    """Download a backup archive."""
    if not _validate_safe_filename(filename):
        from pullbox.core.exceptions import ValidationError

        raise ValidationError(f"Invalid backup filename: {filename}")
    svc = await _get_backup_service(session)
    path = svc.get_backup_path(filename)
    if not path:
        from pullbox.core.exceptions import NotFoundError

        raise NotFoundError("Backup", filename)
    return FileResponse(
        path=str(path),
        media_type="application/zip",
        filename=filename,
    )


@router.delete("/backups/{filename}")
async def delete_backup(
    filename: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, str]:
    """Delete a specific backup archive."""
    if not _validate_safe_filename(filename):
        from pullbox.core.exceptions import ValidationError

        raise ValidationError(f"Invalid backup filename: {filename}")
    svc = await _get_backup_service(session)
    if not svc.delete_backup(filename):
        from pullbox.core.exceptions import NotFoundError

        raise NotFoundError("Backup", filename)
    return {"message": f"Backup deleted: {filename}"}


@router.post("/restore/{filename}", response_model=RestoreResponse)
async def restore_backup(
    filename: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> RestoreResponse:
    """Restore the database from a backup archive.

    The application must be restarted after a successful restore for the
    changes to take effect. The current database is copied into the
    backup directory before being overwritten.
    """
    if not _validate_safe_filename(filename):
        from pullbox.core.exceptions import ValidationError

        raise ValidationError(f"Invalid backup filename: {filename}")
    svc = await _get_backup_runtime_service(session)
    if not await svc.restore_backup(filename):
        from pullbox.core.exceptions import NotFoundError

        raise NotFoundError("Backup", filename)
    return RestoreResponse(
        message=(
            f"Database restored from {filename}. "
            "Restart the application for changes to take effect."
        ),
        restart_required=True,
    )


# ── Restart ───────────────────────────────────────────────────────


@router.post("/restart")
async def restart_system(
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Initiate a graceful application restart.

    Requires interactive authentication (session or local bypass).
    API keys are rejected — restart is too dangerous for automation.
    The process exits with code 42 after a brief delay, and the
    wrapper script (Docker entrypoint, systemd, etc.) relaunches it.
    """
    logger.info("restart_initiated", user="operator")
    shutdown_manager.schedule_restart()
    return {
        "message": "Restart initiated. The application will restart momentarily.",
        "restart_initiated": True,
    }


# ── Update Check ──────────────────────────────────────────────────


@router.get("/updates")
async def get_update_status(
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Return the latest update check result.

    If no cached result exists yet, perform a best-effort lazy check so the
    About page can show a real state on first visit instead of lingering in an
    unchecked placeholder.
    """
    from pullbox.app import get_update_check_service
    from pullbox.services.update_check import UpdateCheckService

    service = get_update_check_service()
    if not isinstance(service, UpdateCheckService):
        return {"checked": False, "error": "Update check service not initialized"}

    result = service.get_cached()
    if result is None:
        try:
            result = await service.check_for_update()
        except Exception:
            logger.warning(
                "lazy_update_status_check_failed",
                subsystem="update_check",
                exc_info=True,
            )
            return {"checked": False}
        if result is None:
            return {"checked": False}

    return {
        "checked": True,
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "release_url": result.release_url,
        "release_notes": result.release_notes,
        "release_date": result.release_date,
        "checked_at": result.checked_at.isoformat() if result.checked_at else None,
    }


@router.post("/updates/check")
async def check_for_update(
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Force a fresh update check against GitHub releases."""
    from pullbox.app import get_update_check_service
    from pullbox.services.update_check import UpdateCheckService

    service = get_update_check_service()
    if not isinstance(service, UpdateCheckService):
        return {"checked": False, "error": "Update check service not initialized"}

    result = await service.check_for_update(force=True)
    if result is None:
        return {"checked": False, "error": "Unable to reach GitHub"}

    return {
        "checked": True,
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "release_url": result.release_url,
        "release_notes": result.release_notes,
        "release_date": result.release_date,
        "checked_at": result.checked_at.isoformat() if result.checked_at else None,
    }


# ── About Route ────────────────────────────────────────────────────


@router.get("/about")
async def get_about(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Return system information for the About page."""
    import platform
    import sys

    import pullbox
    from pullbox.config import get_settings
    from pullbox.ui.routes import get_base_url as _get_base_url

    settings = get_settings()
    build_metadata = get_build_metadata()

    # Python version
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # SQLite version
    import sqlite3

    sqlite_version = sqlite3.sqlite_version

    # SQLAlchemy version
    import sqlalchemy

    sqlalchemy_version = sqlalchemy.__version__

    # Alembic migration count
    migration_count = 0
    try:
        migrations_dir = (
            Path(__file__).resolve().parent.parent.parent.parent.parent / "alembic" / "versions"
        )
        if migrations_dir.is_dir():
            migration_count = len(
                [
                    f
                    for f in migrations_dir.iterdir()
                    if f.suffix == ".py" and not f.name.startswith("__")
                ]
            )
    except Exception:
        pass

    # Docker detection
    is_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

    # Uptime
    uptime_seconds = (datetime.now(UTC) - pullbox.STARTED_AT).total_seconds()

    # Database path (strip SQLAlchemy scheme)
    db_url = settings.db_url
    db_path = db_url.split(":///", 1)[1] if ":///" in db_url else db_url

    # Database file size
    db_size_bytes = None
    try:
        db_file = Path(db_path)
        if db_file.exists():
            db_size_bytes = db_file.stat().st_size
    except Exception:
        pass

    try:
        app_config = await load_system_config_values(session, ("log_level",))
    except TypeError:
        app_config = {"log_level": DEFAULT_SYSTEM_CONFIG["log_level"][0]}

    about: dict[str, object] = {
        "version": pullbox.__version__,
        "release_date": build_metadata.release_date,
        "branch": build_metadata.branch,
        "commit": build_metadata.commit,
        "python_version": python_version,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "os": platform.system(),
        "hostname": platform.node() or None,
        "is_docker": is_docker,
        "database": f"SQLite {sqlite_version}",
        "sqlalchemy_version": sqlalchemy_version,
        "database_path": db_path if settings.debug else Path(db_path).name,
        "database_size_bytes": db_size_bytes,
        "database_migration": migration_count,
        "mode": "Debug" if settings.debug else "Production",
        "uptime_seconds": round(uptime_seconds),
        "started_at": pullbox.STARTED_AT.isoformat(),
        "base_url": _get_base_url(),
        "log_level": app_config.get("log_level", "info"),
        # Links
        "homepage_url": "https://pullbox.app",
        "docs_url": "https://pullbox.app/docs",
        "troubleshooting_url": "https://pullbox.app/docs/reference/troubleshooting",
        "source_url": "https://github.com/pullboxapp/pullbox",
        "issues_url": "https://github.com/pullboxapp/pullbox/issues/new?template=bug_report.yml",
        "discord_url": "https://discord.gg/mg6GQkATaA",
        "bsky_url": "https://bsky.app/profile/pullboxapp.bsky.social",
        "x_url": "https://x.com/PullboxApp",
        "mastodon_url": "https://mastodon.social/@PullboxApp",
        "reddit_url": "https://www.reddit.com/r/Pullbox/",
        # Donations
        "github_sponsors_url": "https://github.com/sponsors/DeusExTaco",
        "open_collective_url": "https://opencollective.com/pullbox",
        "liberapay_url": "https://liberapay.com/DeusExTaco",
        "buymeacoffee_url": "https://buymeacoffee.com/DeusExTaco",
    }

    startup_directory = os.getcwd()
    config_directory = str(settings.data_dir)

    about["application_path"] = startup_directory
    about["startup_directory"] = startup_directory
    about["config_directory"] = config_directory
    about["data_directory"] = config_directory
    about["logs_directory"] = str(settings.logs_dir)
    about["library_root"] = str(settings.library_root)
    about["backups_directory"] = str(settings.backup_dir)

    return about


# ── Comics Directory ──────────────────────────────────────────────


@router.post("/comics-directory", response_model=ComicsDirectoryResponse)
async def set_comics_dir(
    body: ComicsDirectoryRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ComicsDirectoryResponse:
    """Set the primary comics directory for the library."""
    from fastapi import HTTPException

    from pullbox.services.library_service import set_comics_directory

    dir_path = Path(body.path)
    try:
        root = await set_comics_directory(session, dir_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("comics_directory_set", path=root.path, library_root_id=root.id)
    return ComicsDirectoryResponse(path=root.path, library_root_id=root.id)


@router.get("/comics-directory")
async def get_comics_dir(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, str | None]:
    """Get the current comics directory path."""
    from pullbox.services.library_service import get_comics_directory

    path = await get_comics_directory(session)
    return {"path": str(path) if path else None}


# ── Task Routes ────────────────────────────────────────────────────


@router.get("/tasks")
async def list_tasks(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Return all scheduled tasks with execution stats."""
    from pullbox.core.scheduler import get_scheduler

    scheduler = get_scheduler()
    await scheduler.load_persisted_stats(session)
    return {
        "scheduled": scheduler.get_scheduled_tasks(),
    }


@router.post("/tasks/{task_id}/run")
async def run_task(
    task_id: str,
    _user: InteractiveOperatorUser,
) -> dict[str, str]:
    """Queue immediate execution of a scheduled task."""
    from pullbox.core.scheduler import get_scheduler

    scheduler = get_scheduler()
    status = scheduler.run_task_now(task_id)
    if status is None:
        from pullbox.core.exceptions import NotFoundError

        raise NotFoundError("Task", task_id)
    if status == "already_running":
        return {
            "status": status,
            "message": f"Task '{task_id}' is already running.",
        }
    if status == "already_queued":
        return {
            "status": status,
            "message": f"Task '{task_id}' is already queued.",
        }
    return {
        "status": status,
        "message": f"Task '{task_id}' queued.",
    }


# ── Log File Routes ──────────────────────────────────────────────────


router.include_router(system_log_router)


@router.get("/diagnostic-package")
async def download_diagnostic_package(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> FileResponse:
    """Generate and download a diagnostic debug ZIP package.

    Contains sanitized system info, config, health status, database stats,
    recent download history, log files, and installed package versions.
    All secrets are redacted.
    """
    from tempfile import NamedTemporaryFile

    from starlette.background import BackgroundTask

    from pullbox.services.diagnostic_service import create_diagnostic_package

    zip_bytes, filename = await create_diagnostic_package(session)

    # Write to a temp file so FileResponse can stream it
    with NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp.write(zip_bytes)
        tmp.flush()
        tmp_path = tmp.name

    return FileResponse(
        path=tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: Path(tmp_path).unlink(missing_ok=True)),
    )


# ── Debug Logging Override ──────────────────────────────────────────


async def _check_and_clear_expired_override(session: DbSession) -> bool:
    """Check if a debug logging override has expired and clear it if so.

    Returns True if an override was cleared, False otherwise.
    """
    return await check_and_clear_expired_debug_logging_override(session)


@router.get(
    "/debug-logging",
    response_model=DebugLoggingStatusResponse,
)
async def get_debug_logging_status(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> DebugLoggingStatusResponse:
    """Get current debug logging override status."""
    return await get_debug_logging_status_response(session)


@router.post(
    "/debug-logging",
    response_model=DebugLoggingStatusResponse,
)
async def enable_debug_logging(
    body: DebugLoggingRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> DebugLoggingStatusResponse:
    """Enable temporary debug logging with auto-expiry."""
    return await enable_debug_logging_response(body, session)


@router.delete(
    "/debug-logging",
    response_model=DebugLoggingStatusResponse,
)
async def disable_debug_logging(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> DebugLoggingStatusResponse:
    """Disable debug logging override and revert to normal level."""
    return await disable_debug_logging_response(session)
