"""Async orchestration for backup and restore operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pullbox.database import await_maintenance_worker, database_maintenance_window
from pullbox.services.backup_service import BackupInfo, BackupService

if TYPE_CHECKING:
    from pathlib import Path


class BackupRuntimeService:
    """Coordinate backup and restore operations with runtime safety guards."""

    def __init__(self, backup_dir: Path, db_path: Path) -> None:
        self._service = BackupService(backup_dir=backup_dir, db_path=db_path)

    @property
    def service(self) -> BackupService:
        """Expose the underlying synchronous backup service."""
        return self._service

    async def create_backup(self, *, backup_type: str) -> BackupInfo:
        """Create a backup while the database is in a maintenance window."""
        async with database_maintenance_window(reason="backup"):
            return await await_maintenance_worker(
                asyncio.to_thread(self._service.create_backup, backup_type=backup_type)
            )

    async def restore_backup(self, filename: str) -> bool:
        """Restore a backup while the database is paused for maintenance."""
        async with database_maintenance_window(reason="restore_backup"):
            return await await_maintenance_worker(
                asyncio.to_thread(self._service.restore_backup, filename)
            )

    async def cleanup_old_backups(self, *, retention_days: int) -> int:
        """Run retention cleanup off the event loop."""
        return await asyncio.to_thread(
            self._service.cleanup_old_backups,
            retention_days=retention_days,
        )
