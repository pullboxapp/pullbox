"""Cooperative cancellation at safe utility worker boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.exceptions import JobCancelledError

if TYPE_CHECKING:
    from collections.abc import Iterator

_marker: ContextVar[str | None] = ContextVar("utility_cancel_marker", default=None)


@contextmanager
def worker_cancellation(marker: str | None) -> Iterator[None]:
    """Bind a per-pool signal without sharing mutable context between threads."""
    token = _marker.set(marker)
    try:
        yield
    finally:
        _marker.reset(token)


def check_cancelled() -> None:
    """Raise only at boundaries where no source mutation needs to finish."""
    marker = _marker.get()
    if marker is not None and Path(marker).exists():
        raise JobCancelledError("Utility job cancelled")


def cancellation_enabled() -> bool:
    """Whether this call belongs to a cancellable queue worker."""
    return _marker.get() is not None


async def check_cancelled_async() -> None:
    """Adapt the worker signal to interruptible archive operations."""
    check_cancelled()


def check_archive_progress(_stage: str, _current: int, _total: int, _unit: str) -> None:
    """Use archive progress boundaries to stop expensive disposable work."""
    check_cancelled()
