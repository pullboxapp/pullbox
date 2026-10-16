"""Autoflush retains admission ordering for clean and already-locked callers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event, func, select

from pullbox.models import Series
from pullbox.services import library_mutation_coordination as coordination

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase

pytestmark = pytest.mark.parametrize("identity_probe_db", ["postgresql"], indirect=True)


@contextmanager
def _record_admission_and_writes(engine: AsyncEngine) -> Iterator[list[str]]:
    operations: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "pg_advisory_xact_lock(" in statement:
            operations.append("admission")
        elif statement.startswith("INSERT INTO series "):
            operations.append("insert")

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        yield operations
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)


async def _try_admission(session: AsyncSession) -> bool:
    acquired = await session.scalar(
        select(
            func.pg_try_advisory_xact_lock(
                coordination._LOCK_NAMESPACE, coordination._LOCK_RESOURCE
            )
        )
    )
    await session.rollback()
    return bool(acquired)


def _series_count() -> Select[int]:
    return select(func.count()).select_from(Series.__table__)


async def test_clean_admission_precedes_core_query_autoflush(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    engine, factory, _ = identity_probe_db
    with _record_admission_and_writes(engine) as operations:
        async with factory() as owner, factory() as observer:
            assert not owner.new and not owner.dirty and not owner.deleted
            await coordination.lock_file_mutation_admission(owner)
            assert not await _try_admission(observer), "Admission must exclude another transaction"
            pending = Series(title="Clean admission", sort_title="clean admission")
            owner.add(pending)
            assert (await owner.execute(_series_count())).scalar_one() == 1
            assert pending not in owner.new
            assert operations == ["admission", "insert"]
            assert not await _try_admission(observer)
            await owner.rollback()
            assert (await observer.execute(_series_count())).scalar_one() == 0
            assert await _try_admission(observer), "Rollback must release admission"


async def test_repeated_admission_autoflushes_only_after_the_mutex_is_already_held(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    engine, factory, _ = identity_probe_db
    with _record_admission_and_writes(engine) as operations:
        async with factory() as owner, factory() as observer:
            await coordination.lock_file_mutation_admission(owner)
            assert not await _try_admission(observer), "Initial admission must own the mutex"
            pending = Series(title="Repeated admission", sort_title="repeated admission")
            owner.add(pending)
            await coordination.lock_file_mutation_admission(owner)
            assert pending not in owner.new, "The Core advisory-lock query must autoflush"
            assert operations == ["admission", "insert", "admission"]
            assert not await _try_admission(observer)
            await owner.rollback()
            assert (await observer.execute(_series_count())).scalar_one() == 0
            assert await _try_admission(observer), "Rollback must release repeated admission"
