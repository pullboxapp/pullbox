"""Weighted coordination between catalog hydration and ComicInfo enrichment."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

_CATALOGS_PER_COMICINFO_TURN = 3


@dataclass
class _PriorityState:
    condition: asyncio.Condition
    pending_catalogs: int = 0
    catalog_credits: int = 0


_state: _PriorityState | None = None
_state_loop: asyncio.AbstractEventLoop | None = None


def _priority_state() -> _PriorityState:
    global _state, _state_loop
    loop = asyncio.get_running_loop()
    if _state is None or _state_loop is not loop:
        _state = _PriorityState(asyncio.Condition())
        _state_loop = loop
    return _state


def reset_import_metadata_priority() -> None:
    """Reset process-local coordination between tests or event-loop restarts."""
    global _state, _state_loop
    _state = None
    _state_loop = None


class CatalogMetadataWork(AbstractAsyncContextManager["CatalogMetadataWork"]):
    """Track a known catalog batch and expose per-series completion checkpoints."""

    def __init__(self, units: int) -> None:
        self._units = max(units, 0)
        self._remaining = self._units
        self._state: _PriorityState | None = None

    async def __aenter__(self) -> CatalogMetadataWork:
        self._state = _priority_state()
        async with self._state.condition:
            if self._state.pending_catalogs == 0:
                self._state.catalog_credits = 0
            self._state.pending_catalogs += self._units
            self._state.condition.notify_all()
        return self

    async def complete_one(self) -> None:
        if self._state is None or self._remaining == 0:
            return
        async with self._state.condition:
            self._remaining -= 1
            self._state.pending_catalogs -= 1
            self._state.catalog_credits += 1
            self._state.condition.notify_all()

    async def __aexit__(self, *_exc: object) -> None:
        assert self._state is not None
        async with self._state.condition:
            self._state.pending_catalogs -= self._remaining
            self._remaining = 0
            if self._state.pending_catalogs == 0:
                self._state.catalog_credits = 0
            self._state.condition.notify_all()


def catalog_metadata_work(units: int) -> CatalogMetadataWork:
    """Register catalog work before it competes for ComicVine capacity."""
    return CatalogMetadataWork(units)


async def wait_for_comicinfo_turn() -> None:
    """Give each three catalog completions priority over one new file update."""
    state = _priority_state()
    async with state.condition:
        await state.condition.wait_for(
            lambda: (
                state.pending_catalogs == 0 or state.catalog_credits >= _CATALOGS_PER_COMICINFO_TURN
            )
        )
        if state.pending_catalogs == 0:
            state.catalog_credits = 0
        else:
            state.catalog_credits -= _CATALOGS_PER_COMICINFO_TURN
