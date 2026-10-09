"""Fast schema setup for fresh, disposable SQLite test databases only.

Cache SQL generated from the real models, not an engine or populated database.
Every caller still owns a new connection and independent transaction state.
Migration tests and tests that change model metadata must use normal DDL instead.
"""

from typing import TYPE_CHECKING, cast

import aiosqlite
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncConnection

from pullbox.models import Base

if TYPE_CHECKING:
    from sqlite3 import Connection


class SQLiteTestSchema:
    """Reuse schema construction, never a test's engine, connection, or data."""

    def __init__(self) -> None:
        self._script: str | None = None

    async def create(self, connection: AsyncConnection) -> None:
        if connection.dialect.name != "sqlite":
            raise ValueError("Cached test schema requires SQLite")
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        if not isinstance(driver, aiosqlite.Connection):
            raise ValueError("Cached test schema requires the aiosqlite driver")
        # executescript commits a pending sqlite transaction. Never silently
        # commit caller work, even when SQLAlchemy's logical transaction is new.
        if driver.in_transaction:
            raise ValueError("Cached test schema requires no active SQLite transaction")
        existing = await connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
        )
        if existing.first() is not None:
            raise ValueError("Cached test schema requires an empty database")

        if self._script is None:
            template = create_engine("sqlite:///:memory:")
            try:
                Base.metadata.create_all(template, checkfirst=False)
                with template.connect() as source:
                    sqlite = cast("Connection", source.connection.driver_connection)
                    self._script = "\n".join(sqlite.iterdump())
            finally:
                template.dispose()

        cursor = await driver.executescript(self._script)
        await cursor.close()


create_test_schema = SQLiteTestSchema().create
