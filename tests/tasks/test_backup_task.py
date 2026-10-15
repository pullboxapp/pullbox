"""Tests for the scheduled backup task — interval logic, config reading, error resilience.

Covers:
- Creates backup when no previous exists
- Skips backup when interval not elapsed
- Creates backup when interval has elapsed
- Runs cleanup after backup
- Reads config from database (non-default values)
- Uses defaults when DB has no config rows
- Continues cleanup when backup creation fails
- Logs error when backup creation fails

Run:
    pytest tests/tasks/test_backup_task.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.config import SystemConfig
from pullbox.services.backup_service import (
    BACKUP_EXTENSION,
    BACKUP_PREFIX,
    DB_FILENAME,
    METADATA_FILENAME,
    BackupService,
)
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-backup-task")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def real_db(tmp_path: Path) -> Path:
    """Create a real SQLite database file for BackupService to back up."""
    path = tmp_path / "pullbox.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def _create_old_scheduled_backup(
    backup_dir: Path,
    age_days: int = 0,
) -> Path:
    """Create a scheduled backup ZIP with a specific age."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC) - timedelta(days=age_days)
    filename = f"{BACKUP_PREFIX}{ts.strftime('%Y%m%d_%H%M%S')}{BACKUP_EXTENSION}"
    zip_path = backup_dir / filename
    meta = {
        "pullbox_version": "0.5.0",
        "created_at": ts.isoformat(),
        "backup_type": "scheduled",
        "db_size_bytes": 1000,
    }
    with ZipFile(zip_path, "w") as zf:
        zf.writestr(METADATA_FILENAME, json.dumps(meta))
        zf.writestr(DB_FILENAME, "fake db")
    if age_days > 0:
        old_time = time.time() - (age_days * 86400)
        os.utime(zip_path, (old_time, old_time))
    return zip_path


async def _seed_config(
    factory: async_sessionmaker[AsyncSession],
    interval_days: str = "7",
    retention_days: str = "28",
) -> None:
    """Seed backup schedule config into SystemConfig."""
    async with factory() as session:
        for key, value in [
            ("backup_interval_days", interval_days),
            ("backup_retention_days", retention_days),
        ]:
            session.add(SystemConfig(key=key, value=value, value_type="string"))
        await session.commit()


def _runtime_settings(backup_dir: Path) -> SimpleNamespace:
    """Return the minimal runtime settings object used by backup_task."""
    return SimpleNamespace(backup_dir=backup_dir)


# ── Tests ──────────────────────────────────────────────────────────────


class TestRunBackups:
    """Tests for the run_backups() scheduled task."""

    @pytest.mark.asyncio
    async def test_creates_backup_when_no_previous_exists(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
        ):
            await run_backups()

        zips = list(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_EXTENSION}"))
        assert len(zips) == 1

    @pytest.mark.asyncio
    async def test_skips_backup_when_interval_not_elapsed(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory, interval_days="7")
        # Create a recent scheduled backup (1 day old)
        _create_old_scheduled_backup(backup_dir, age_days=1)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
        ):
            await run_backups()

        zips = list(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_EXTENSION}"))
        assert len(zips) == 1  # Still just the one we created

    @pytest.mark.asyncio
    async def test_creates_backup_when_interval_elapsed(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory, interval_days="7")
        # Create an old scheduled backup (10 days old)
        _create_old_scheduled_backup(backup_dir, age_days=10)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
        ):
            await run_backups()

        zips = list(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_EXTENSION}"))
        assert len(zips) == 2  # Old one + new one

    @pytest.mark.asyncio
    async def test_uses_default_config_when_db_empty(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        tmp_path: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        # Don't seed any config — task should use defaults
        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(tmp_path / "backups"),
            ),
            patch("pullbox.services.backup_service.BackupService.create_backup") as mock_create,
            patch(
                "pullbox.services.backup_service.BackupService.list_backups",
                return_value=[],
            ),
            patch("pullbox.services.backup_service.BackupService.cleanup_old_backups"),
        ):
            await run_backups()

        mock_create.assert_called_once_with(backup_type="scheduled")

    @pytest.mark.asyncio
    async def test_continues_cleanup_when_backup_fails(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.create_backup",
                side_effect=RuntimeError("disk full"),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.list_backups",
                return_value=[],
            ),
            patch(
                "pullbox.services.backup_service.BackupService.cleanup_old_backups"
            ) as mock_cleanup,
            pytest.raises(RuntimeError, match="disk full"),
        ):
            await run_backups()

        # Cleanup should still run even though backup creation failed
        mock_cleanup.assert_called_once_with(retention_days=28)

    @pytest.mark.asyncio
    async def test_reads_non_default_config_from_db(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(
            db_factory,
            interval_days="1",
            retention_days="14",
        )

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.list_backups",
                return_value=[],
            ),
            patch("pullbox.services.backup_service.BackupService.create_backup"),
            patch(
                "pullbox.services.backup_service.BackupService.cleanup_old_backups"
            ) as mock_cleanup,
        ):
            await run_backups()

        # Should use the DB-configured retention of 14 days, not the default 28
        mock_cleanup.assert_called_once_with(retention_days=14)

    @pytest.mark.asyncio
    async def test_loads_backup_policy_with_shared_config_resolver(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.tasks import backup_task

        calls: list[tuple[str, ...]] = []

        async def fake_load_system_config_values(
            session: AsyncSession,
            keys: tuple[str, ...],
        ) -> dict[str, str]:
            _ = session
            calls.append(keys)
            return {
                "backup_interval_days": "2",
                "backup_retention_days": "11",
            }

        monkeypatch.setattr(
            backup_task,
            "load_system_config_values",
            fake_load_system_config_values,
            raising=False,
        )

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.list_backups",
                return_value=[],
            ),
            patch("pullbox.services.backup_service.BackupService.create_backup"),
            patch(
                "pullbox.services.backup_service.BackupService.cleanup_old_backups"
            ) as mock_cleanup,
        ):
            await backup_task.run_backups()

        assert calls == [("backup_interval_days", "backup_retention_days")]
        mock_cleanup.assert_called_once_with(retention_days=11)

    @pytest.mark.asyncio
    async def test_runs_cleanup_after_successful_backup(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
        ):
            await run_backups()

        # Backup was created and cleanup ran (no exceptions)
        zips = list(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_EXTENSION}"))
        assert len(zips) >= 1

    @pytest.mark.asyncio
    async def test_manual_run_bypasses_interval_gate_and_creates_manual_backup(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory, interval_days="7")
        _create_old_scheduled_backup(backup_dir, age_days=1)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch("pullbox.tasks.backup_task.get_current_task_trigger_type", return_value="manual"),
        ):
            await run_backups()

        backups = BackupService(backup_dir=backup_dir, db_path=real_db).list_backups()
        assert len(backups) == 2
        assert backups[0].backup_type == "manual"

    @pytest.mark.asyncio
    async def test_invalid_last_backup_timestamp_creates_new_backup(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory, interval_days="7")

        invalid_backup = SimpleNamespace(
            backup_type="scheduled",
            created_at="not-a-real-date",
        )

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.list_backups",
                return_value=[invalid_backup],
            ),
        ):
            await run_backups()

        zips = list(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_EXTENSION}"))
        assert len(zips) == 1

    @pytest.mark.asyncio
    async def test_cleanup_failure_is_raised_after_backup_attempt(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        real_db: Path,
        backup_dir: Path,
    ) -> None:
        from pullbox.tasks.backup_task import run_backups

        await _seed_config(db_factory)

        with (
            patch("pullbox.tasks.backup_task.get_session_factory", return_value=db_factory),
            patch("pullbox.tasks.backup_task._resolve_db_path", return_value=real_db),
            patch(
                "pullbox.tasks.backup_task.get_settings",
                return_value=_runtime_settings(backup_dir),
            ),
            patch(
                "pullbox.services.backup_service.BackupService.cleanup_old_backups",
                side_effect=RuntimeError("cleanup failed"),
            ),
            pytest.raises(RuntimeError, match="cleanup failed"),
        ):
            await run_backups()


class TestResolveDbPath:
    """Backup DB-path extraction should tolerate the DB URL shapes we use."""

    def test_resolve_db_path_prefers_triple_slash_sqlite_url(self) -> None:
        from pullbox.tasks.backup_task import _resolve_db_path

        with patch(
            "pullbox.tasks.backup_task.get_settings",
            return_value=SimpleNamespace(db_url="sqlite+aiosqlite:////data/pullbox.db"),
        ):
            assert _resolve_db_path() == Path("/data/pullbox.db")

    def test_resolve_db_path_falls_back_to_double_slash_form(self) -> None:
        from pullbox.tasks.backup_task import _resolve_db_path

        with patch(
            "pullbox.tasks.backup_task.get_settings",
            return_value=SimpleNamespace(db_url="sqlite://relative/pullbox.db"),
        ):
            assert _resolve_db_path() == Path("relative/pullbox.db")
