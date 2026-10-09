"""Schema setup may be cached; test database state must never be shared."""

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from pullbox.models import Base
from tests.sqlite_schema import SQLiteTestSchema


@pytest.fixture
async def fresh_engines() -> AsyncGenerator[list[AsyncEngine], None]:
    engines = [create_async_engine("sqlite+aiosqlite:///:memory:") for _ in range(2)]
    try:
        yield engines
    finally:
        for engine in engines:
            await engine.dispose()


async def _schema(engine: AsyncEngine) -> list[tuple]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
        )
        return [tuple(row) for row in result]


async def test_cached_schema_matches_all_orm_tables_indexes_and_constraints(fresh_engines):
    standard, optimized = fresh_engines
    async with standard.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with optimized.begin() as connection:
        await SQLiteTestSchema().create(connection)
    assert await _schema(optimized) == await _schema(standard)


async def test_model_ddl_is_built_once_for_two_fresh_databases(fresh_engines):
    schema = SQLiteTestSchema()
    with patch.object(Base.metadata, "create_all", wraps=Base.metadata.create_all) as build:
        for engine in fresh_engines:
            async with engine.begin() as connection:
                await schema.create(connection)
    assert build.call_count == 1, "Repeated fixture setup still rebuilds the ORM schema"


async def test_concurrent_initialization_uses_one_empty_schema(fresh_engines):
    schema = SQLiteTestSchema()

    async def initialize(engine):
        async with engine.begin() as connection:
            await schema.create(connection)

    with patch.object(Base.metadata, "create_all", wraps=Base.metadata.create_all) as build:
        await asyncio.gather(*(initialize(engine) for engine in fresh_engines))
    assert build.call_count == 1
    assert await _schema(fresh_engines[0]) == await _schema(fresh_engines[1])


async def test_non_sqlite_connection_is_rejected_before_access():
    connection = MagicMock(spec=AsyncConnection)
    connection.dialect = SimpleNamespace(name="postgresql")
    with pytest.raises(ValueError, match="requires SQLite"):
        await SQLiteTestSchema().create(connection)
    connection.get_raw_connection.assert_not_called()


async def test_failed_schema_build_does_not_poison_next_database(fresh_engines):
    schema = SQLiteTestSchema()
    async with fresh_engines[0].begin() as connection:
        with (
            patch.object(Base.metadata, "create_all", side_effect=RuntimeError("DDL failure")),
            pytest.raises(RuntimeError, match="DDL failure"),
        ):
            await schema.create(connection)
    assert not await _schema(fresh_engines[0])
    async with fresh_engines[1].begin() as connection:
        await schema.create(connection)
    assert len(await _schema(fresh_engines[1])) > len(Base.metadata.tables)


async def test_committed_rows_and_schema_changes_do_not_leak(fresh_engines):
    first, second = fresh_engines
    schema = SQLiteTestSchema()
    async with first.begin() as connection:
        await schema.create(connection)
        await connection.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active) "
                "VALUES ('first', 'unused', 1)"
            )
        )
        await connection.execute(text("CREATE TABLE test_only_state (id INTEGER)"))
    async with second.begin() as connection:
        await schema.create(connection)
        assert (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one() == 0
        assert not (
            await connection.execute(
                text("SELECT 1 FROM sqlite_master WHERE name = 'test_only_state'")
            )
        ).all()
        # A committed insert in another test cannot consume our first primary key.
        await connection.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active) "
                "VALUES ('second', 'unused', 1)"
            )
        )
        assert (await connection.execute(text("SELECT id FROM users"))).scalar_one() == 1


@pytest.mark.parametrize("table_name", ["sentinel", "sqliteXsentinel"])
async def test_nonempty_database_is_rejected_without_mutation(fresh_engines, table_name):
    engine = fresh_engines[0]
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE TABLE {table_name} (value TEXT)"))
        await connection.execute(text(f"INSERT INTO {table_name} VALUES ('keep')"))
    before = await _schema(engine)
    async with engine.begin() as connection:
        with pytest.raises(ValueError, match="empty"):
            await SQLiteTestSchema().create(connection)
        assert (
            await connection.execute(text(f"SELECT value FROM {table_name}"))
        ).scalar_one() == "keep"
    assert await _schema(engine) == before


async def test_explicit_sqlite_transaction_is_not_committed_by_setup(fresh_engines):
    async with fresh_engines[0].connect() as connection:
        await connection.execute(text("BEGIN"))
        with pytest.raises(ValueError, match="transaction"):
            await SQLiteTestSchema().create(connection)
        raw = await connection.get_raw_connection()
        assert raw.driver_connection.in_transaction
        await connection.rollback()


async def test_connection_pragmas_and_constraints_remain_enforced(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'isolated.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def connection_settings(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=12345")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    try:
        async with engine.begin() as connection:
            await SQLiteTestSchema().create(connection)
            for name, expected in (
                ("foreign_keys", 1),
                ("busy_timeout", 12345),
                ("journal_mode", "wal"),
            ):
                assert (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar_one() == expected
            await connection.execute(
                text(
                    "INSERT INTO users (username, password_hash, is_active) "
                    "VALUES ('unique', 'unused', 1)"
                )
            )
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError, match="UNIQUE constraint"):
                await connection.execute(
                    text(
                        "INSERT INTO users (username, password_hash, is_active) "
                        "VALUES ('unique', 'unused', 1)"
                    )
                )
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint"):
                await connection.execute(
                    text(
                        "INSERT INTO api_keys (user_id, key_hash, name, is_active) "
                        "VALUES (-1, 'bad', 'orphan', 1)"
                    )
                )
    finally:
        await engine.dispose()
