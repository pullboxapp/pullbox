"""Disposable database parity lane; never reads runtime database settings."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from pullbox.models import Base
from tests.fixtures.metadata_identity_persistence import build_identity_schema_probe
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.fixture(params=["sqlite", "postgresql"])
async def identity_probe_db(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[IdentityProbeDatabase, None]:
    backend = request.param
    if backend == "postgresql":
        configured = os.environ.get("PULLBOX_METADATA_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("Set PULLBOX_METADATA_TEST_POSTGRES_URL for disposable PostgreSQL parity")
        url = make_url(configured)
        if (
            url.drivername != "postgresql+asyncpg"
            or url.database != "pullbox_metadata_contract_test"
        ):
            pytest.fail("PostgreSQL identity tests require the dedicated contract-test database")
        schema = f"mp0_test_{uuid4().hex}"
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(CreateSchema(schema))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}")

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(connection: Any, _record: object) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA journal_mode=WAL")

    probe = build_identity_schema_probe()
    try:
        async with engine.begin() as connection:
            if backend == "sqlite":
                await create_test_schema(connection)
            else:
                await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(probe.series.create)
            await connection.run_sync(probe.issue.create)
            if probe.arc_index is not None:
                await connection.run_sync(probe.arc_index.create)
            for table in probe.events.values():
                await connection.run_sync(table.create)
        yield engine, async_sessionmaker(engine, expire_on_commit=False), probe
    finally:
        await engine.dispose()
        if backend == "postgresql":
            try:
                async with admin.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
            finally:
                await admin.dispose()
