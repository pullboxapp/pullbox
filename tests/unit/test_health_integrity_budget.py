"""SQLite integrity health checks must have a database-enforced time budget."""

import sqlite3
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from pullbox.models.health import HealthStatus
from pullbox.services import health_database_checks as checks


@pytest.mark.asyncio
async def test_integrity_does_not_run_unbounded_sql_on_shared_health_session(tmp_path):
    path = tmp_path / "health.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with AsyncSession(engine) as session:
            session.execute = AsyncMock(side_effect=AssertionError("Unbounded health SQL"))
            result = None
            with suppress(AssertionError):
                result = await checks.check_db_integrity(session)
            assert result is not None, (
                "Integrity checking must use its own bounded read-only connection"
            )
            assert result.status == HealthStatus.HEALTHY
            session.execute.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_integrity_budget_exhaustion_is_not_reported_as_corruption(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")
        connection.executemany("INSERT INTO sample VALUES (?)", [(i,) for i in range(2000)])
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(checks, "_INTEGRITY_BUDGET_SECONDS", 0.0, raising=False)
    try:
        async with AsyncSession(engine) as session:
            result = await checks.check_db_integrity(session)
        assert result is not None
        assert result.status == HealthStatus.DEGRADED
        assert "budget" in result.message.lower()
        assert "corrupt" not in result.message.lower()
    finally:
        await engine.dispose()
