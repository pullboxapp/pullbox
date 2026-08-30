"""Bounded batch allocation contracts for the import rollback journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import StatementError

from pullbox.models.import_job import (
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services import import_job_actions as journal

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/synthetic/batch-journal",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    session.add(job)
    await session.flush()
    return job


def _spec(ordinal: int) -> journal.ImportJobActionSpec:
    return journal.ImportJobActionSpec(
        phase=f"phase-{ordinal}",
        action_type=f"action-{ordinal}",
        payload={"ordinal": ordinal},
    )


async def test_record_actions_preserves_order_allocates_one_block_and_feeds_single_writer(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    await journal.record_action(
        db_session,
        job,
        phase="seed",
        action_type="seed-action",
        payload={"seed": True},
    )
    specifications = [_spec(1), _spec(2), _spec(3)]

    actions = await journal.record_actions(db_session, job, specifications)
    following = await journal.record_action(
        db_session,
        job,
        phase="following",
        action_type="following-action",
        payload={"following": True},
    )

    assert [action.sequence_no for action in actions] == [2, 3, 4]
    assert [action.phase for action in actions] == [spec.phase for spec in specifications]
    assert [action.action_type for action in actions] == [
        spec.action_type for spec in specifications
    ]
    assert [action.payload for action in actions] == [spec.payload for spec in specifications]
    assert following.sequence_no == 5
    persisted = list(
        (
            await db_session.scalars(
                select(ImportJobAction)
                .where(ImportJobAction.import_job_id == job.id)
                .order_by(ImportJobAction.sequence_no.asc())
            )
        ).all()
    )
    assert [action.sequence_no for action in persisted] == [1, 2, 3, 4, 5]


async def test_record_actions_is_caller_transaction_owned_and_rolls_back_atomically(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    job_id = job.id
    await db_session.commit()

    actions = await journal.record_actions(db_session, job, [_spec(1), _spec(2)])
    assert [action.sequence_no for action in actions] == [1, 2]
    assert db_session.in_transaction()

    await db_session.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count(ImportJobAction.id)).where(
                    ImportJobAction.import_job_id == job_id
                )
            )
            or 0
        )
        == 0
    )


async def test_record_actions_failure_does_not_advance_session_sequence_cache(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    seed = ImportJobAction(
        import_job_id=job.id,
        sequence_no=4,
        phase="seed",
        action_type="seed-action",
        payload={"seed": True},
    )
    db_session.add(seed)
    job_id = job.id
    await db_session.commit()
    invalid = journal.ImportJobActionSpec(
        phase="broken",
        action_type="broken-action",
        payload={"not_json": object()},
    )

    with pytest.raises(StatementError):
        await journal.record_actions(db_session, job, [invalid, _spec(2)])
    await db_session.rollback()
    job = await db_session.get(ImportJob, job_id)
    assert job is not None

    following = await journal.record_action(
        db_session,
        job,
        phase="following",
        action_type="following-action",
        payload={"following": True},
    )
    assert following.sequence_no == 5


async def test_record_actions_has_bounded_non_returning_fallback(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db_session)
    monkeypatch.setattr(journal, "_supports_multirow_insert_returning", lambda _session: False)

    actions = await journal.record_actions(db_session, job, [_spec(1), _spec(2)])

    assert [action.sequence_no for action in actions] == [1, 2]
    assert [action.payload for action in actions] == [{"ordinal": 1}, {"ordinal": 2}]


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_batch_insert_statement_compiles_as_portable_executemany_returning(
    dialect: Dialect,
) -> None:
    statement = journal._action_insert_statement(returning=True)

    sql = str(statement.compile(dialect=dialect)).upper()

    assert sql.startswith("INSERT INTO IMPORT_JOB_ACTIONS")
    assert "RETURNING" in sql
    assert "ON CONFLICT" not in sql


async def test_record_actions_query_count_is_constant_within_one_bounded_batch(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    job = await _job(db_session)
    await db_session.commit()
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        sql = str(args[2]).lstrip().upper()
        if sql.startswith(("SELECT", "INSERT")):
            statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        actions = await journal.record_actions(
            db_session,
            job,
            [_spec(ordinal) for ordinal in range(1, 201)],
        )
        following = await journal.record_action(
            db_session,
            job,
            phase="following",
            action_type="following-action",
            payload={"following": True},
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(actions) == 200
    assert following.sequence_no == 201
    assert sum(sql.startswith("SELECT") for sql in statements) == 1
    assert sum(sql.startswith("INSERT") for sql in statements) == 2


async def test_record_actions_pages_oversized_input_into_bounded_bulk_inserts(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    job = await _job(db_session)
    await db_session.commit()
    insert_statements: list[str] = []

    def record_statement(*args: object) -> None:
        sql = str(args[2]).lstrip().upper()
        if sql.startswith("INSERT"):
            insert_statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        actions = await journal.record_actions(
            db_session,
            job,
            [_spec(ordinal) for ordinal in range(1, journal._ACTION_INSERT_BATCH_SIZE + 2)],
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(actions) == journal._ACTION_INSERT_BATCH_SIZE + 1
    assert len(insert_statements) == 2


async def test_record_actions_empty_batch_is_a_noop(db_session: AsyncSession) -> None:
    job = await _job(db_session)

    assert await journal.record_actions(db_session, job, []) == []
    assert journal._action_sequence_cache(db_session) == {}
