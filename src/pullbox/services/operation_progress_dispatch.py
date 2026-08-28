"""Coalesced detached persistence for high-frequency progress events."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from pullbox.database import get_session_factory
from pullbox.services.operation_progress import (
    OperationProgressUpdate,
    publish_operation_progress,
)
from pullbox.utilities.sse import publish

if TYPE_CHECKING:
    from collections.abc import Hashable

logger = structlog.get_logger(__name__)

_COALESCE_SECONDS = 0.5
_pending_updates: dict[tuple[Hashable, str], OperationProgressUpdate] = {}
_flush_tasks: dict[tuple[Hashable, str], asyncio.Task[None]] = {}
_state_lock = asyncio.Lock()


def _identity(update: OperationProgressUpdate) -> tuple[Hashable, str]:
    return (update.operation_type, update.operation_key)


async def notify_activity_changed(update: OperationProgressUpdate) -> None:
    """Publish a small invalidation after a durable or live progress change."""
    await publish(
        "activity",
        "progress",
        {
            "operation_type": update.operation_type.value,
            "operation_key": update.operation_key,
            "revision": update.revision,
        },
    )


async def queue_operation_progress(update: OperationProgressUpdate) -> None:
    """Coalesce high-frequency updates while keeping the latest revision durable."""
    identity = _identity(update)
    async with _state_lock:
        current = _pending_updates.get(identity)
        current_revision = current.revision if current is not None else None
        incoming_revision = update.revision
        if (
            current is None
            or incoming_revision is None
            or current_revision is None
            or incoming_revision > current_revision
        ):
            _pending_updates[identity] = update
        task = _flush_tasks.get(identity)
        if task is None or task.done():
            task = asyncio.create_task(_flush_operation_progress(identity))
            _flush_tasks[identity] = task


async def _flush_operation_progress(identity: tuple[Hashable, str]) -> None:
    try:
        while True:
            await asyncio.sleep(_COALESCE_SECONDS)
            async with _state_lock:
                update = _pending_updates.pop(identity, None)
            if update is None:
                return
            factory = get_session_factory()
            async with factory() as session:
                try:
                    await publish_operation_progress(session, update)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception(
                        "operation_progress_detached_persist_failed",
                        operation_type=update.operation_type.value,
                        operation_key=update.operation_key,
                    )
                    return
            await notify_activity_changed(update)
            async with _state_lock:
                if identity not in _pending_updates:
                    return
    finally:
        async with _state_lock:
            current = _flush_tasks.get(identity)
            if current is asyncio.current_task():
                _flush_tasks.pop(identity, None)


async def drain_operation_progress_updates() -> None:
    """Wait for pending detached writes, primarily for tests and shutdowns."""
    while True:
        async with _state_lock:
            tasks = [task for task in _flush_tasks.values() if not task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
