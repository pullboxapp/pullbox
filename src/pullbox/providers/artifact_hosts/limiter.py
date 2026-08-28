"""Fair global and per-host concurrency bounds for artifact transfers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pullbox.models.direct_acquisition import DirectArtifactHostKind
from pullbox.providers.artifact_hosts.transport_contract import (
    ArtifactTransferCancelledError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


class ArtifactTransferLimiter:
    """Prevent one host's waiters from consuming unrelated global capacity."""

    def __init__(
        self,
        *,
        global_limit: int,
        per_host_limit: int = 1,
        host_limits: Mapping[DirectArtifactHostKind, int] | None = None,
    ) -> None:
        if global_limit < 1:
            raise ValueError("Global artifact transfer limit must be at least 1.")
        if per_host_limit < 1:
            raise ValueError("Default per-host transfer limit must be at least 1.")
        overrides = dict(host_limits or {})
        if any(limit < 1 for limit in overrides.values()):
            raise ValueError("Every artifact host transfer limit must be at least 1.")

        self._global = asyncio.Semaphore(global_limit)
        self._hosts = {
            host_kind: asyncio.Semaphore(overrides.get(host_kind, per_host_limit))
            for host_kind in DirectArtifactHostKind
        }

    @asynccontextmanager
    async def slot(
        self,
        host_kind: DirectArtifactHostKind,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        """Acquire the host permit first so its queue cannot starve other hosts."""
        host = self._hosts[host_kind]
        await _acquire_with_cancel(host, cancel_event)
        global_acquired = False
        try:
            await _acquire_with_cancel(self._global, cancel_event)
            global_acquired = True
            yield
        finally:
            if global_acquired:
                self._global.release()
            host.release()


class DirectProviderTransferLimiter:
    """Serialize remote acquisition work independently for each direct provider."""

    def __init__(self, *, per_provider_limit: int = 1) -> None:
        if per_provider_limit < 1:
            raise ValueError("Default per-provider transfer limit must be at least 1.")
        self._per_provider_limit = per_provider_limit
        self._providers: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(
        self,
        provider_identity: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        """Wait for one provider lane without contacting the remote provider."""
        normalized_identity = provider_identity.strip()
        if not normalized_identity:
            raise ValueError("Direct provider identity cannot be empty.")
        provider = self._providers.setdefault(
            normalized_identity,
            asyncio.Semaphore(self._per_provider_limit),
        )
        await _acquire_with_cancel(provider, cancel_event)
        try:
            yield
        finally:
            provider.release()


async def _acquire_with_cancel(
    semaphore: asyncio.Semaphore,
    cancel_event: asyncio.Event | None,
) -> None:
    if cancel_event is None:
        await semaphore.acquire()
        return
    if cancel_event.is_set():
        raise ArtifactTransferCancelledError

    acquire_task = asyncio.create_task(semaphore.acquire())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {acquire_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        acquire_task.cancel()
        cancel_task.cancel()
        await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
        raise
    if cancel_task in done:
        if acquire_task in done and acquire_task.result():
            semaphore.release()
        else:
            acquire_task.cancel()
        await asyncio.gather(acquire_task, return_exceptions=True)
        raise ArtifactTransferCancelledError

    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)
