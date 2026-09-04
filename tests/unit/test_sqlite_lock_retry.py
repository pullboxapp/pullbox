"""Tests for shared SQLite transaction retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import OperationalError

from pullbox.core.sqlite_lock import run_sqlite_transaction_with_retry


async def test_sqlite_transaction_retries_lock_after_rollback() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    operation = AsyncMock(
        side_effect=[
            OperationalError("UPDATE downloads", {}, Exception("database is locked")),
            "applied",
        ]
    )
    sleep = AsyncMock()
    logger = MagicMock()

    result = await run_sqlite_transaction_with_retry(
        session,
        operation,
        event_name="download_monitor_write",
        logger=logger,
        retry_delay=lambda _attempt: 0.25,
        sleep=sleep,
        attempts=3,
    )

    assert result == "applied"
    assert operation.await_count == 2
    session.rollback.assert_awaited_once_with()
    session.commit.assert_awaited_once_with()
    sleep.assert_awaited_once_with(0.25)
    logger.warning.assert_called_once()


async def test_sqlite_transaction_does_not_retry_other_operational_errors() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    error = OperationalError("UPDATE downloads", {}, Exception("disk I/O error"))
    operation = AsyncMock(side_effect=error)

    try:
        await run_sqlite_transaction_with_retry(
            session,
            operation,
            event_name="download_monitor_write",
            logger=MagicMock(),
            sleep=AsyncMock(),
        )
    except OperationalError as exc:
        assert exc is error
    else:
        raise AssertionError("Expected the non-locking OperationalError to propagate")

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()
