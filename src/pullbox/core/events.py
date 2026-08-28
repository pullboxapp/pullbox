"""In-process async event bus and MVP event definitions.

Provides decoupled communication between components.  Handlers subscribe
to specific event types and are invoked when that event is emitted.
Handler failures are isolated — one broken handler never prevents others
from running.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable  # noqa: TC003 - used at runtime in type annotations
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - used at runtime in type annotations
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event Bus
# ---------------------------------------------------------------------------


class EventBus:
    """Simple async event bus for in-process event distribution.

    Handlers may be sync or async callables.  Each handler receives the
    emitted event as its sole argument.  If a handler raises, the error
    is logged and remaining handlers still execute.
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[..., Any]]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Callable[..., Any]) -> None:
        """Register *handler* to be called whenever *event_type* is emitted."""
        self._handlers[event_type].append(handler)

    async def emit(self, event: object) -> None:
        """Dispatch *event* to all subscribed handlers.

        Handlers are called sequentially.  Errors are logged but never
        propagated — one failing handler does not block the rest.
        """
        event_type = type(event)
        handlers = self._handlers.get(event_type, [])
        log = logger.bind(event_name=event_type.__name__, handler_count=len(handlers))
        log.debug("event_emitted")

        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or inspect.isawaitable(result):
                    await result
            # noinspection PyBroadException
            except Exception:
                logger.exception(
                    "event_handler_error",
                    event_name=event_type.__name__,
                    handler=getattr(handler, "__name__", repr(handler)),
                )


# ---------------------------------------------------------------------------
# MVP Event Definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesAdded:
    """Emitted when a new series is added to the library."""

    series_id: int
    comicvine_id: int


@dataclass(frozen=True)
class IssueWanted:
    """Emitted when an issue is marked as wanted."""

    issue_id: int
    series_id: int


@dataclass(frozen=True)
class DownloadStarted:
    """Emitted when a download is sent to a client."""

    download_id: int
    issue_id: int
    client_type: str


@dataclass(frozen=True)
class DownloadCompleted:
    """Emitted when a download finishes successfully."""

    download_id: int
    issue_id: int
    file_path: str


@dataclass(frozen=True)
class DownloadFailed:
    """Emitted when a download fails."""

    download_id: int
    issue_id: int
    error: str


@dataclass(frozen=True)
class FileMatched:
    """Emitted when a library file is matched to an issue."""

    library_file_id: int
    issue_id: int
    confidence: str


@dataclass(frozen=True)
class HealthCheckCompleted:
    """Emitted after a health check runs."""

    component: str
    status: str
    message: str


@dataclass(frozen=True)
class ReaderCompletionChanged:
    """Emitted after one user's explicit completion state commits."""

    user_id: int
    issue_id: int
    state_version: int
    completed: bool
    occurred_at: datetime
    origin: str


@dataclass(frozen=True)
class ReaderWantToReadChanged:
    """Emitted after one user's private reading queue state commits."""

    user_id: int
    issue_id: int
    state_version: int
    enabled: bool
    occurred_at: datetime


# ---------------------------------------------------------------------------
# Module-level Singleton
# ---------------------------------------------------------------------------

_event_bus_instance: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the application event bus singleton.

    Creates the ``EventBus`` on first call.  Subsequent calls return
    the same instance so all subscribers and emitters share one bus.
    """
    global _event_bus_instance
    if _event_bus_instance is None:
        _event_bus_instance = EventBus()
    return _event_bus_instance
