"""Pending ORM writes must respect maintenance admission for every query form."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import pullbox.database as database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from sqlalchemy import Select
    from sqlalchemy.sql.elements import TextClause


class AutoflushProbeBase(DeclarativeBase):
    pass


class AutoflushProbe(AutoflushProbeBase):
    __tablename__ = "autoflush_probe"

    id: Mapped[int] = mapped_column(primary_key=True)


type StatementKind = Literal["text", "core", "orm"]


def _count_statement(kind: StatementKind) -> Select[int] | TextClause:
    if kind == "text":
        return text("SELECT count(*) FROM autoflush_probe")
    if kind == "core":
        return select(func.count()).select_from(AutoflushProbe.__table__)
    return select(func.count(AutoflushProbe.id))


@pytest.fixture
async def autoflush_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[async_sessionmaker[database.GateAwareAsyncSession], list[str]], None]:
    gate = asyncio.Event()
    gate.set()
    monkeypatch.setattr(database, "_maintenance_gate", gate)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'autoflush.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(AutoflushProbeBase.metadata.create_all)
    statements: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        yield async_sessionmaker(engine, class_=database.GateAwareAsyncSession), statements
    finally:
        gate.set()
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
        await engine.dispose()


@pytest.mark.parametrize("statement_kind", ["text", "core", "orm"])
async def test_autoflush_waits_for_maintenance_and_remains_rollbackable(
    autoflush_database: tuple[async_sessionmaker[database.GateAwareAsyncSession], list[str]],
    monkeypatch: pytest.MonkeyPatch,
    statement_kind: StatementKind,
) -> None:
    factory, statements = autoflush_database
    admitted = asyncio.Event()
    wait_for_ready = database.wait_for_database_ready

    async def observe_admission() -> None:
        admitted.set()
        await wait_for_ready()

    monkeypatch.setattr(database, "wait_for_database_ready", observe_admission)
    async with factory() as session:
        pending = AutoflushProbe(id=1)
        session.add(pending)
        assert pending in session.new
        database._maintenance_gate.clear()
        read_task = asyncio.create_task(session.execute(_count_statement(statement_kind)))
        admission_task = asyncio.create_task(admitted.wait())
        try:
            completed, _ = await asyncio.wait(
                (read_task, admission_task), timeout=2, return_when=asyncio.FIRST_COMPLETED
            )
            assert admission_task in completed, "Query bypassed maintenance admission"
            assert not read_task.done(), "Query completed while maintenance was active"
            assert statements == [], "Autoflush issued SQL before maintenance admission"
            database._maintenance_gate.set()
            result = await asyncio.wait_for(read_task, 2)
            assert result.scalar_one() == 1, "All query forms must autoflush pending ORM writes"
            assert pending not in session.new
            await session.rollback()
            assert (await session.execute(_count_statement("core"))).scalar_one() == 0
        finally:
            database._maintenance_gate.set()
            admission_task.cancel()
            read_task.cancel()
            await asyncio.gather(read_task, admission_task, return_exceptions=True)


@pytest.mark.parametrize("statement_kind", ["text", "core", "orm"])
async def test_explicit_no_autoflush_preserves_pending_writes_until_an_orm_query(
    autoflush_database: tuple[async_sessionmaker[database.GateAwareAsyncSession], list[str]],
    statement_kind: StatementKind,
) -> None:
    factory, _ = autoflush_database
    async with factory() as session:
        pending = AutoflushProbe(id=1)
        session.add(pending)
        with session.no_autoflush:
            assert (await session.execute(_count_statement(statement_kind))).scalar_one() == 0
        assert pending in session.new
        assert (await session.execute(_count_statement("orm"))).scalar_one() == 1
        assert pending not in session.new
        await session.rollback()
        assert (await session.execute(_count_statement("core"))).scalar_one() == 0
