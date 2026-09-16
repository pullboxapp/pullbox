"""Shared helpers for transient SQLite write-lock retries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SQLITE_LOCK_RETRY_ATTEMPTS = 8
SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS = 0.25


def is_sqlite_locked_error(exc: BaseException) -> bool:
    """Return whether an operational error is SQLite write-lock contention."""
    message = str(exc).lower()
    return "database is locked" in message or "locking protocol" in message


def sqlite_lock_retry_delay(attempt: int) -> float:
    """Return the linear backoff delay for a SQLite lock retry attempt."""
    return SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS * attempt


async def run_sqlite_transaction_with_retry[T](
    session: Any,
    operation: Callable[[], Awaitable[T]],
    *,
    event_name: str,
    logger: Any,
    retry_delay: Callable[[int], float] = sqlite_lock_retry_delay,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    attempts: int = SQLITE_LOCK_RETRY_ATTEMPTS,
) -> T:
    """Commit a short transaction, retrying only transient SQLite lock failures."""
    for attempt in range(1, attempts + 1):
        try:
            result = await operation()
            await session.commit()
            return result
        except OperationalError as exc:
            await session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == attempts:
                raise
            delay_seconds = retry_delay(attempt)
            logger.warning(
                f"{event_name}_retrying_after_sqlite_lock",
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=delay_seconds,
            )
            await sleep(delay_seconds)
    raise RuntimeError("SQLite transaction retry loop ended unexpectedly")
