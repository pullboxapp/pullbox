"""Bounded, application-local provider candidate snapshots."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pullbox.schemas.metadata_sources import (
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SourceStatus,
    StoryArcDiscoveryQuery,
    StoryArcDiscoveryRead,
)
from pullbox.services.metadata_sources import SourceRuntime


class MetadataSearchBusyError(RuntimeError):
    """Too many different interactive metadata searches are already running."""


def discovery_cache_key(
    query: SeriesDiscoveryQuery | StoryArcDiscoveryQuery,
    runtime: Sequence[SourceRuntime],
    *,
    catalog_generation: str | None,
    gcd_api_enabled: bool,
    gcd_generation: str | None = None,
) -> str:
    request = query.model_dump(mode="json")
    request["query"] = " ".join(query.query.split()).casefold()
    if query.sources is not None:
        request["sources"] = sorted(query.sources)
    payload = {
        "contract": "series-search-v1"
        if isinstance(query, SeriesDiscoveryQuery)
        else "arc-search-v1",
        "query": request,
        "sources": [
            {
                **item.policy.model_dump(
                    mode="json",
                    include={
                        "source",
                        "revision",
                        "enabled",
                        "priority",
                        "domain_priorities",
                        "credential_configured",
                        "configuration_status",
                    },
                ),
                "unavailable": item.unavailable,
            }
            for item in sorted(runtime, key=lambda item: item.policy.source)
        ],
        "catalog_generation": catalog_generation,
        "gcd_generation": gcd_generation,
        "gcd_api_enabled": gcd_api_enabled,
    }
    # This digest identifies public search/configuration snapshots, not credentials.
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class _Flight:
    task: asyncio.Task[bytes]
    waiters: int = 0


class MetadataSearchCache:
    def __init__(
        self,
        *,
        max_entries: int = 32,
        max_bytes: int = 8 * 1024 * 1024,
        max_pending: int = 4,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not 1 <= max_entries <= 128 or not 1 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("Search snapshot storage must be bounded")
        if not 1 <= max_pending <= 8:
            raise ValueError("Search admission must be bounded")
        self.max_entries, self.max_bytes, self.max_pending = max_entries, max_bytes, max_pending
        self.clock = clock or time.monotonic
        self._entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._pending: dict[str, _Flight] = {}

    def _discard(self, key: str) -> None:
        _, payload = self._entries.pop(key)
        self._bytes -= len(payload)

    async def _load(
        self,
        key: str,
        loader: Callable[[], Awaitable[SeriesDiscoveryRead | StoryArcDiscoveryRead]],
        *,
        cache_result: bool,
    ) -> bytes:
        result = await loader()
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        payload = result.model_dump_json().encode()
        if cache_result and len(payload) <= self.max_bytes:
            while self._entries and (
                len(self._entries) >= self.max_entries
                or self._bytes + len(payload) > self.max_bytes
            ):
                self._discard(next(iter(self._entries)))
            complete = bool(result.results) and all(
                source.status in {SourceStatus.OK, SourceStatus.EMPTY} and not source.truncated
                for source in result.sources
            )
            self._entries[key] = (self.clock() + (300 if complete else 30), payload)
            self._bytes += len(payload)
        return payload

    async def get(
        self,
        key: str,
        loader: Callable[[], Awaitable[SeriesDiscoveryRead]],
        *,
        cache_result: bool = True,
    ) -> SeriesDiscoveryRead:
        return SeriesDiscoveryRead.model_validate_json(
            await self._get_payload(key, loader, cache_result=cache_result)
        )

    async def get_arcs(
        self,
        key: str,
        loader: Callable[[], Awaitable[StoryArcDiscoveryRead]],
        *,
        cache_result: bool = True,
    ) -> StoryArcDiscoveryRead:
        return StoryArcDiscoveryRead.model_validate_json(
            await self._get_payload(key, loader, cache_result=cache_result)
        )

    async def _get_payload(
        self,
        key: str,
        loader: Callable[[], Awaitable[SeriesDiscoveryRead | StoryArcDiscoveryRead]],
        *,
        cache_result: bool,
    ) -> bytes:
        for expired in [key for key, (until, _) in self._entries.items() if until <= self.clock()]:
            self._discard(expired)
        cached = self._entries.get(key) if cache_result else None
        if cached is not None:
            self._entries.move_to_end(key)
            return cached[1]
        flight = self._pending.get(key)
        if flight is not None and flight.task.cancelling():
            raise MetadataSearchBusyError("The previous search is stopping. Try again shortly.")
        if flight is None:
            if len(self._pending) >= self.max_pending:
                raise MetadataSearchBusyError(
                    "Other metadata searches are running. Try again shortly."
                )
            flight = _Flight(
                asyncio.create_task(self._load(key, loader, cache_result=cache_result))
            )
            self._pending[key] = flight
        flight.waiters += 1
        try:
            payload = await asyncio.shield(flight.task)
            return payload
        finally:
            flight.waiters -= 1
            if not flight.waiters:
                if not flight.task.done():
                    flight.task.cancel()
                try:
                    await asyncio.gather(flight.task, return_exceptions=True)
                finally:
                    if self._pending.get(key) is flight:
                        self._pending.pop(key)
