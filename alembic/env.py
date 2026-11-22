"""Alembic environment — async SQLAlchemy migration runner."""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so autogenerate detects them.
from pullbox.models import Base
from pullbox.services.migration_first_start import require_startable_target

config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from the PULLBOX_DB_URL environment variable.
db_url = os.environ.get("PULLBOX_DB_URL", "")
if db_url:
    # ConfigParser interpolates percent signs; SQLAlchemy needs the original URL escapes.
    config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits SQL to stdout without connecting to the database.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure context and run migrations within a connection."""
    if connection.dialect.name == "postgresql":
        already_in_transaction = connection.in_transaction()
        require_startable_target(
            connection, stage_initializer=config.attributes.get("closed_stage_initializer") is True
        )
        if not already_in_transaction:
            connection.rollback()
    # SQLite table rebuilds must not cascade into existing child tables. Keep
    # embedded Alembic callers consistent with the standalone migration process.
    sqlite_fk_enabled = False
    if connection.dialect.name == "sqlite":
        sqlite_fk_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)

    try:
        with context.begin_transaction():
            context.run_migrations()
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        if sqlite_fk_enabled:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def run_async_migrations() -> None:
    """Create an async engine and run migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an async engine."""
    connection = config.attributes.get("connection")
    if connection is not None:
        # An offline transfer must stay on the one session admitted before the target closed.
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
