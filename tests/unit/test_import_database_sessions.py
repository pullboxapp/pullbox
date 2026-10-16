"""Import session selection follows SQLite URL semantics, not rendered spelling."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import URL, event, literal, select
from sqlalchemy.exc import SAWarning
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import ORMExecuteState, Session

from pullbox.core.exceptions import JobPausedError
from pullbox.services.import_file_matching import _spool_grouped_cohort_rows
from pullbox.services.import_job_controls import raise_if_job_cancelled_immediately
from pullbox.services.import_job_execution_progress import progress_session_factory_for_runtime
from pullbox.services.import_provider_cache import is_in_memory_sqlite_engine

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("driver", "database", "query", "expected"),
    [
        ("sqlite+aiosqlite", None, {}, True),
        ("sqlite+aiosqlite", "", {}, True),
        ("sqlite+aiosqlite", ":memory:", {}, True),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "memory", "uri": "true"}, True),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "memory", "uri": " YES "}, True),
        ("sqlite+aiosqlite", "file::memory:", {"uri": "1"}, True),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "memory"}, False),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "memory", "uri": "false"}, False),
        ("sqlite+aiosqlite", "file::memory:", {}, False),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "ro", "uri": "true"}, False),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "rw", "uri": "true"}, False),
        ("sqlite+aiosqlite", "file:import-probe", {"mode": "rwc", "uri": "true"}, False),
        ("sqlite+aiosqlite", "file:import-probe", {"note": "mode=memory", "uri": "true"}, False),
        ("sqlite+aiosqlite", "local:memory:.db", {}, False),
        ("sqlite+aiosqlite", "mode=memory.db", {}, False),
        ("postgresql+asyncpg", ":memory:", {}, False),
        ("postgresql+asyncpg", "file:import-probe", {"mode": "memory", "uri": "true"}, False),
    ],
)
async def test_memory_classifier_uses_structured_url(
    driver: str, database: str | None, query: dict[str, str], expected: bool
) -> None:
    url = URL.create(driver, database=database, query=query)
    if (
        driver == "sqlite+aiosqlite"
        and query.get("mode") == "memory"
        and query.get("uri")
        in {
            None,
            "false",
        }
    ):
        with pytest.warns(SAWarning, match="SQLite URI arguments require"):
            engine = create_async_engine(url)
    else:
        engine = create_async_engine(url)
    try:
        assert is_in_memory_sqlite_engine(engine) is expected
    finally:
        await engine.dispose()


def _test_url(kind: str, tmp_path: Path) -> URL:
    if kind == "memory":
        return URL.create("sqlite+aiosqlite", database=":memory:")
    if kind == "empty":
        return URL.create("sqlite+aiosqlite")
    if kind == "uri-memory":
        return URL.create(
            "sqlite+aiosqlite",
            database=f"file:{tmp_path / 'named-memory'}",
            query={"mode": "memory", "cache": "shared", "uri": "true"},
        )
    filename = "local:memory:.db" if kind == "file-colon" else "local.db"
    return URL.create("sqlite+aiosqlite", database=str(tmp_path / filename))


@pytest.mark.parametrize("kind", ["memory", "empty", "uri-memory", "file", "file-colon"])
async def test_control_sessions_preserve_memory_or_file_isolation(
    kind: str, tmp_path: Path
) -> None:
    engine = create_async_engine(_test_url(kind, tmp_path))
    memory = kind in {"memory", "empty", "uri-memory"}
    seen: list[AsyncSession] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as active:
            connection = await active.connection()
            original_dbapi = connection.sync_connection.connection.dbapi_connection

            async def check_cancel(session: AsyncSession, job_id: int) -> None:
                seen.append(session)
                assert job_id == 37
                assert (session is active) is memory
                control_connection = await session.connection()
                assert (
                    control_connection.sync_connection.connection.dbapi_connection is original_dbapi
                ) is memory
                assert await session.scalar(select(literal(17))) == 17
                if memory:
                    assert session.autoflush is False
                raise JobPausedError("paused")

            with pytest.raises(JobPausedError, match="paused"):
                await raise_if_job_cancelled_immediately(
                    active, 37, raise_if_cancelled=check_cancel
                )
            assert len(seen) == 1
            assert active.autoflush is True
            assert await active.scalar(select(literal(19))) == 19
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["memory", "empty", "uri-memory", "file", "file-colon"])
async def test_progress_sessions_preserve_memory_or_file_isolation(
    kind: str, tmp_path: Path
) -> None:
    engine = create_async_engine(_test_url(kind, tmp_path))
    memory = kind in {"memory", "empty", "uri-memory"}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as active:
            connection = await active.connection()
            original_dbapi = connection.sync_connection.connection.dbapi_connection
            progress_factory = progress_session_factory_for_runtime(active)
            if memory:
                assert progress_factory is None
            else:
                assert progress_factory is not None
                async with progress_factory() as progress:
                    assert progress is not active
                    assert type(progress) is type(active)
                    progress_connection = await progress.connection()
                    assert (
                        progress_connection.sync_connection.connection.dbapi_connection
                        is not original_dbapi
                    )
                    assert await progress.scalar(select(literal(23))) == 23
    finally:
        await engine.dispose()


async def test_connection_bound_memory_session_keeps_active_control_and_progress(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(_test_url("memory", tmp_path))
    try:
        async with engine.connect() as connection, AsyncSession(connection) as active:

            async def check_cancel(session: AsyncSession, _job_id: int) -> None:
                assert session is active
                assert await session.scalar(select(literal(29))) == 29

            await raise_if_job_cancelled_immediately(active, 37, raise_if_cancelled=check_cancel)
            assert progress_session_factory_for_runtime(active) is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["memory", "empty", "uri-memory", "file", "file-colon"])
async def test_grouped_cohort_scan_reuses_memory_session_or_isolates_file_reader(
    kind: str, tmp_path: Path
) -> None:
    engine = create_async_engine(_test_url(kind, tmp_path))
    memory = kind in {"memory", "empty", "uri-memory"}
    observed: list[Session] = []
    cancelled_checks: list[int] = []

    def record_scan(state: ORMExecuteState) -> None:
        if state.execution_options.get("pullbox_cohort_scan") == "url-regression":
            observed.append(state.session)

    event.listen(Session, "do_orm_execute", record_scan)
    try:
        async with async_sessionmaker(engine)() as active:

            async def check_cancel(session: AsyncSession, job_id: int) -> None:
                assert session is active
                assert job_id == 37
                cancelled_checks.append(job_id)

            spool = await _spool_grouped_cohort_rows(
                active,
                job_id=37,
                query=select(literal(1)).union_all(select(literal(2))),
                batch_size=1,
                scan_name="url-regression",
                row_payload=lambda row: [row[0]],
                raise_if_cancelled=check_cancel,
            )
            with spool:
                assert [json.loads(line) for line in spool] == [[1], [2]]
            assert len(observed) == 1
            assert (observed[0] is active.sync_session) is memory
            assert cancelled_checks == [37, 37, 37, 37]
            assert await active.scalar(select(literal(31))) == 31
    finally:
        event.remove(Session, "do_orm_execute", record_scan)
        await engine.dispose()
