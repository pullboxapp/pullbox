"""Backup background task — automated scheduled backups with retention cleanup.

Runs daily, checks if the configured backup interval has elapsed since the
last scheduled backup, creates a new one if so, and prunes old scheduled
backups that exceed the retention period.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from pullbox.config import get_settings
from pullbox.core.backup_storage import sqlite_backup_path
from pullbox.core.config_resolver import get_int_setting, load_system_config_values
from pullbox.core.scheduler import get_current_task_trigger_type, scheduled_task
from pullbox.database import get_session_factory
from pullbox.services.backup_runtime_service import BackupRuntimeService

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

_BACKUP_POLICY_KEYS = ("backup_interval_days", "backup_retention_days")
_DEFAULT_BACKUP_INTERVAL_DAYS = 7
_DEFAULT_BACKUP_RETENTION_DAYS = 28


def _resolve_db_path() -> Path | None:
    return sqlite_backup_path(get_settings().db_url)


@scheduled_task(
    task_id="run_backups",
    trigger="cron",
    display_name="Backup",
    available=lambda: _resolve_db_path() is not None,
    hour=3,
    exclusive=True,
)
async def run_backups() -> None:
    """Create a scheduled backup if the interval has elapsed, then enforce retention."""
    db_path = _resolve_db_path()
    if db_path is None:
        return
    factory = get_session_factory()
    trigger_type = get_current_task_trigger_type()

    async with factory() as session:
        try:
            # Read backup policy from DB; backup path comes from bootstrap settings.
            cfg = await load_system_config_values(session, _BACKUP_POLICY_KEYS)
            backup_dir = get_settings().backup_dir
            interval_days = get_int_setting(
                cfg,
                "backup_interval_days",
                _DEFAULT_BACKUP_INTERVAL_DAYS,
            )
            retention_days = get_int_setting(
                cfg,
                "backup_retention_days",
                _DEFAULT_BACKUP_RETENTION_DAYS,
            )
        except Exception:
            await session.rollback()
            raise

    runtime_svc = BackupRuntimeService(backup_dir=backup_dir, db_path=db_path)
    svc = runtime_svc.service

    should_backup = True
    backup_type = "manual" if trigger_type == "manual" else "scheduled"
    if trigger_type != "manual":
        # Scheduled runs honor the interval gate. Manual runs should create a
        # real backup immediately so the Tasks page action matches user intent.
        existing = svc.list_backups()
        last_scheduled = next(
            (b for b in existing if b.backup_type == "scheduled"),
            None,
        )

        if last_scheduled and last_scheduled.created_at:
            try:
                last_dt = datetime.fromisoformat(last_scheduled.created_at)
                if datetime.now(UTC) - last_dt < timedelta(days=interval_days):
                    should_backup = False
                    logger.debug(
                        "backup_skipped",
                        reason="interval not elapsed",
                        last_backup=last_scheduled.created_at,
                        interval_days=interval_days,
                    )
            except (ValueError, TypeError):
                pass  # Can't parse date, create a backup to be safe

    backup_error: Exception | None = None
    if should_backup:
        try:
            await runtime_svc.create_backup(backup_type=backup_type)
        except Exception as exc:
            backup_error = exc
            logger.exception("scheduled_backup_failed", subsystem="backup")

    # Enforce retention (runs even if backup failed — cleanup frees disk space)
    cleanup_error: Exception | None = None
    try:
        await runtime_svc.cleanup_old_backups(retention_days=retention_days)
    except Exception as exc:
        cleanup_error = exc
        logger.exception("backup_cleanup_failed", subsystem="backup")

    if backup_error is not None:
        raise backup_error
    if cleanup_error is not None:
        raise cleanup_error
