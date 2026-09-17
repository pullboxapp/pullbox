"""Maintenance must drain existing transactions without trapping their commits."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullbox.database as database


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_maintenance_gate", asyncio.Event())
    database._maintenance_gate.set()
    monkeypatch.setattr(database, "_maintenance_lock", asyncio.Lock())
    monkeypatch.setattr(database, "dispose_engine", AsyncMock())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'maintenance.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
    yield async_sessionmaker(engine, class_=database.GateAwareAsyncSession)
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_writer_can_commit_before_maintenance_but_new_reads_wait(factory):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def maintain():
        async with database.database_maintenance_window(reason="test"):
            entered.set()
            await release.wait()

    async with factory() as writer, factory() as reader:
        await writer.execute(text("INSERT INTO sample VALUES (1)"))
        task = asyncio.create_task(maintain())
        while database._maintenance_gate.is_set():
            await asyncio.sleep(0)
        read_task = asyncio.create_task(reader.execute(text("SELECT count(*) FROM sample")))
        try:
            assert not entered.is_set(), "Maintenance started over an active writer"
            await asyncio.wait_for(writer.commit(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            assert not read_task.done()
            release.set()
            await task
            assert (await read_task).scalar() == 1
        finally:
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await read_task


@pytest.mark.asyncio
async def test_busy_maintenance_defers_and_reopens_database(factory, monkeypatch):
    monkeypatch.setattr(database, "_MAINTENANCE_DRAIN_SECONDS", 0.01, raising=False)
    error = None
    async with factory() as writer:
        await writer.execute(text("INSERT INTO sample VALUES (1)"))
        try:
            async with database.database_maintenance_window(reason="test"):
                pass
        except RuntimeError as exc:
            error = exc
        assert error is not None, "Maintenance must defer rather than run over an active writer"
        assert "busy" in str(error).lower()
        assert database._maintenance_gate.is_set()
        assert database.database_maintenance_reason() is None
        database.dispose_engine.assert_not_awaited()
        await writer.commit()


@pytest.mark.asyncio
async def test_cancelled_maintenance_drain_reopens_database(factory):
    async with factory() as writer:
        await writer.execute(text("INSERT INTO sample VALUES (1)"))

        async def maintain():
            async with database.database_maintenance_window(reason="test"):
                await asyncio.Event().wait()

        task = asyncio.create_task(maintain())
        while database._maintenance_gate.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert database._maintenance_gate.is_set()
        await asyncio.wait_for(writer.commit(), 1)
