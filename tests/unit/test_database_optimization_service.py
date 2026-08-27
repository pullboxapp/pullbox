"""Tests for SQLite database optimization and its maintenance runtime."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from pullbox.services.database_optimization_service import (
    DatabaseOptimizationRuntimeService,
    DatabaseOptimizationService,
)

if TYPE_CHECKING:
    from pathlib import Path


def _create_fragmented_database(path: Path) -> None:
    """Create enough free pages for VACUUM to have observable work."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payloads (id INTEGER PRIMARY KEY, body BLOB NOT NULL)")
    connection.executemany(
        "INSERT INTO payloads (body) VALUES (?)",
        [(b"x" * 4096,) for _ in range(96)],
    )
    connection.commit()
    connection.execute("DELETE FROM payloads")
    connection.commit()
    connection.close()


def test_preview_reports_reclaimable_free_pages(tmp_path: Path) -> None:
    db_path = tmp_path / "pullbox.db"
    _create_fragmented_database(db_path)

    preview = DatabaseOptimizationService(db_path).preview()

    assert preview.database_bytes > 0
    assert preview.free_pages > 0
    assert preview.reclaimable_bytes > 0
    assert preview.required_free_bytes == preview.database_bytes
    assert preview.integrity_result == "ok"


def test_optimize_checkpoints_and_vacuums_free_pages(tmp_path: Path) -> None:
    db_path = tmp_path / "pullbox.db"
    _create_fragmented_database(db_path)
    service = DatabaseOptimizationService(db_path)
    before = service.preview()

    result = service.optimize()

    assert before.free_pages > 0
    assert result.before.free_pages == before.free_pages
    assert result.after.free_pages == 0
    assert result.reclaimed_bytes > 0
    assert result.after.database_bytes < result.before.database_bytes
    assert result.after.integrity_result == "ok"


def test_maintain_reindexes_then_optimizes_and_verifies(tmp_path: Path) -> None:
    db_path = tmp_path / "pullbox.db"
    _create_fragmented_database(db_path)
    statements: list[str] = []

    class RecordingCursor:
        def __init__(self, row: tuple[object, ...] | None = None) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class RecordingConnection:
        def __enter__(self) -> RecordingConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> RecordingCursor:
            statements.append(statement)
            if statement == "PRAGMA wal_checkpoint(TRUNCATE)":
                return RecordingCursor((0, 0, 0))
            if statement == "PRAGMA quick_check":
                return RecordingCursor(("ok",))
            return RecordingCursor()

    service = DatabaseOptimizationService(db_path)
    service._connect = lambda *, read_only: RecordingConnection()  # type: ignore[method-assign]

    result = service.maintain()

    assert statements == [
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "REINDEX",
        "PRAGMA optimize=0x10002",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA quick_check",
    ]
    assert result.integrity_result == "ok"


def test_maintain_refreshes_planner_statistics_on_real_database(tmp_path: Path) -> None:
    db_path = tmp_path / "pullbox.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL)")
    connection.execute("CREATE INDEX ix_issues_series_id ON issues (series_id)")
    connection.executemany(
        "INSERT INTO issues (series_id) VALUES (?)",
        [(series_id % 7,) for series_id in range(200)],
    )
    connection.commit()
    connection.close()

    result = DatabaseOptimizationService(db_path).maintain()

    connection = sqlite3.connect(db_path)
    statistics = connection.execute(
        "SELECT stat FROM sqlite_stat1 WHERE idx = 'ix_issues_series_id'"
    ).fetchone()
    connection.close()
    assert result.integrity_result == "ok"
    assert statistics is not None


@pytest.mark.asyncio
async def test_runtime_uses_maintenance_window_and_thread_offload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pullbox.db"
    _create_fragmented_database(db_path)
    runtime = DatabaseOptimizationRuntimeService(db_path)
    reasons: list[str] = []
    thread_call: dict[str, object] = {}

    @asynccontextmanager
    async def fake_window(*, reason: str):  # type: ignore[no-untyped-def]
        reasons.append(reason)
        yield

    async def fake_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        thread_call["func"] = func
        thread_call["args"] = args
        thread_call["kwargs"] = kwargs
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "pullbox.services.database_optimization_service.database_maintenance_window",
        fake_window,
    )
    monkeypatch.setattr(
        "pullbox.services.database_optimization_service.asyncio.to_thread",
        fake_to_thread,
    )

    result = await runtime.optimize()

    assert reasons == ["database_optimize"]
    assert thread_call["func"] == runtime.service.optimize
    assert result.reclaimed_bytes > 0


@pytest.mark.asyncio
async def test_nightly_runtime_uses_maintenance_window_and_thread_offload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pullbox.db"
    _create_fragmented_database(db_path)
    runtime = DatabaseOptimizationRuntimeService(db_path)
    reasons: list[str] = []
    thread_call: dict[str, object] = {}

    @asynccontextmanager
    async def fake_window(*, reason: str):  # type: ignore[no-untyped-def]
        reasons.append(reason)
        yield

    async def fake_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        thread_call["func"] = func
        thread_call["args"] = args
        thread_call["kwargs"] = kwargs
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "pullbox.services.database_optimization_service.database_maintenance_window",
        fake_window,
    )
    monkeypatch.setattr(
        "pullbox.services.database_optimization_service.asyncio.to_thread",
        fake_to_thread,
    )

    result = await runtime.maintain()

    assert reasons == ["nightly_database_maintenance"]
    assert thread_call["func"] == runtime.service.maintain
    assert result.integrity_result == "ok"
