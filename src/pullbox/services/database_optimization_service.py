"""SQLite database compaction with an application-wide maintenance window."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pullbox.database import database_maintenance_window

_BUSY_TIMEOUT_MS = 30_000


class DatabaseOptimizationError(RuntimeError):
    """Raised when SQLite database optimization cannot run safely."""


@dataclass(frozen=True, slots=True)
class DatabaseOptimizationPreview:
    """Current SQLite storage state and the capacity needed to compact it."""

    database_bytes: int
    wal_bytes: int
    page_count: int
    free_pages: int
    page_size: int
    reclaimable_bytes: int
    required_free_bytes: int
    available_free_bytes: int
    integrity_result: str


@dataclass(frozen=True, slots=True)
class DatabaseOptimizationResult:
    """Verified outcome of a database optimization run."""

    before: DatabaseOptimizationPreview
    after: DatabaseOptimizationPreview

    @property
    def reclaimed_bytes(self) -> int:
        """Return main-database bytes reclaimed by VACUUM."""
        return max(0, self.before.database_bytes - self.after.database_bytes)


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceResult:
    """Verified outcome of lightweight recurring SQLite maintenance."""

    integrity_result: str


class DatabaseOptimizationService:
    """Inspect and compact one SQLite database file."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def preview(self) -> DatabaseOptimizationPreview:
        """Return current free-list and disk-capacity information without mutation."""
        self._ensure_database_exists()
        with self._connect(read_only=True) as connection:
            return self._build_preview(connection)

    def optimize(self) -> DatabaseOptimizationResult:
        """Checkpoint WAL data, compact free pages, and verify the result."""
        before = self.preview()
        if before.available_free_bytes < before.required_free_bytes:
            raise DatabaseOptimizationError(
                "Not enough free disk space to optimize the database. "
                f"Need at least {before.required_free_bytes} additional bytes."
            )

        with self._connect(read_only=False) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise DatabaseOptimizationError(
                    "SQLite could not checkpoint the write-ahead log because the database is busy."
                )
            connection.execute("VACUUM")

        after = self.preview()
        if after.integrity_result.lower() != "ok":
            raise DatabaseOptimizationError(
                f"SQLite integrity verification failed after optimization: {after.integrity_result}"
            )
        return DatabaseOptimizationResult(before=before, after=after)

    def maintain(self) -> DatabaseMaintenanceResult:
        """Rebuild indexes, refresh planner statistics, and verify integrity."""
        self._ensure_database_exists()
        with self._connect(read_only=False) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise DatabaseOptimizationError(
                    "SQLite could not checkpoint the write-ahead log because the database is busy."
                )
            connection.execute("REINDEX")
            # This maintenance connection has no query history, so include the
            # all-tables mask recommended by SQLite for a fresh connection.
            connection.execute("PRAGMA optimize=0x10002")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise DatabaseOptimizationError(
                    "SQLite could not checkpoint the write-ahead log after maintenance."
                )
            integrity_row = connection.execute("PRAGMA quick_check").fetchone()

        integrity_result = str(integrity_row[0] if integrity_row else "unknown")
        if integrity_result.lower() != "ok":
            raise DatabaseOptimizationError(
                f"SQLite integrity verification failed after maintenance: {integrity_result}"
            )
        return DatabaseMaintenanceResult(integrity_result=integrity_result)

    def _ensure_database_exists(self) -> None:
        if not self._db_path.is_file():
            raise DatabaseOptimizationError("The configured SQLite database does not exist.")

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
        else:
            connection = sqlite3.connect(
                str(self._db_path),
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _build_preview(self, connection: sqlite3.Connection) -> DatabaseOptimizationPreview:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        integrity_row = connection.execute("PRAGMA quick_check").fetchone()
        integrity_result = str(integrity_row[0] if integrity_row else "unknown")
        database_bytes = self._db_path.stat().st_size
        wal_path = Path(f"{self._db_path}-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
        free_bytes = shutil.disk_usage(self._db_path.parent).free

        return DatabaseOptimizationPreview(
            database_bytes=database_bytes,
            wal_bytes=wal_bytes,
            page_count=page_count,
            free_pages=free_pages,
            page_size=page_size,
            reclaimable_bytes=free_pages * page_size,
            # SQLite needs room for a replacement database while retaining the original.
            required_free_bytes=database_bytes,
            available_free_bytes=free_bytes,
            integrity_result=integrity_result,
        )


class DatabaseOptimizationRuntimeService:
    """Run SQLite compaction while all Pullbox database activity is paused."""

    def __init__(self, db_path: Path) -> None:
        self._service = DatabaseOptimizationService(db_path)

    @property
    def service(self) -> DatabaseOptimizationService:
        """Expose the synchronous service for inspection and focused tests."""
        return self._service

    async def optimize(self) -> DatabaseOptimizationResult:
        """Compact the database outside the event loop under the maintenance gate."""
        async with database_maintenance_window(reason="database_optimize"):
            return await asyncio.to_thread(self._service.optimize)

    async def maintain(self) -> DatabaseMaintenanceResult:
        """Run recurring maintenance outside the event loop under the shared gate."""
        async with database_maintenance_window(reason="nightly_database_maintenance"):
            return await asyncio.to_thread(self._service.maintain)
