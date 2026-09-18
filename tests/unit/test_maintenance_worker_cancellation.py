"""Cancellation cannot reopen the database while a maintenance thread still runs."""

import asyncio
import threading
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

import pullbox.database as database
from pullbox.services.backup_runtime_service import BackupRuntimeService
from pullbox.services.database_optimization_service import DatabaseOptimizationRuntimeService


@pytest.mark.parametrize("operation", ["backup", "restore", "optimize", "maintain"])
async def test_cancellation_keeps_gate_closed_until_thread_finishes(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setattr(database, "_maintenance_gate", asyncio.Event())
    database._maintenance_gate.set()
    monkeypatch.setattr(database, "_maintenance_lock", asyncio.Lock())
    monkeypatch.setattr(database, "dispose_engine", AsyncMock())
    started = threading.Event()
    release = threading.Event()

    def worker(*args, **kwargs):
        started.set()
        release.wait(3)

    if operation in {"backup", "restore"}:
        runtime = BackupRuntimeService(tmp_path / "backups", tmp_path / "db")
        name = f"{operation}_backup" if operation == "restore" else "create_backup"
        monkeypatch.setattr(runtime.service, name, worker)
        coro = (
            runtime.restore_backup("test.zip")
            if operation == "restore"
            else runtime.create_backup(backup_type="manual")
        )
    else:
        runtime = DatabaseOptimizationRuntimeService(tmp_path / "db")
        monkeypatch.setattr(runtime.service, operation, worker)
        coro = getattr(runtime, operation)()
    task = asyncio.create_task(coro)
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not database._maintenance_gate.is_set(), (
            "A live SQLite worker lost its maintenance fence"
        )
        assert not task.done()
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await task
    assert database._maintenance_gate.is_set()
