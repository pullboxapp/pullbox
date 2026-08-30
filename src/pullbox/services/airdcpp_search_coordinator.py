"""Bounded subscribe-before-send AirDC++ search coordination."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.issue_numbers import format_issue_number
from pullbox.providers.airdcpp.contracts import (
    AirDcppSearchResult,
    AirDcppSearchResultEvent,
    AirDcppSearchSentEvent,
)
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_search_types import (
    AirDcppSearchProgress,
    AirDcppSearchProgressState,
    DcClientSearchStatus,
    DcClientSearchSummary,
    DcMetrics,
    DcRoute,
    DcSearchOutcome,
    DcValidatedCandidate,
)
from pullbox.services.release_validator import ReleaseValidator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from pullbox.providers.airdcpp.contracts import AirDcppSearchInstance
    from pullbox.services.airdcpp_search_cooldown import AirDcppCooldownReservation
    from pullbox.services.search_targets import IssueSearchTarget
    from pullbox.services.search_types import ValidatorKwargs

_SEARCH_EXTENSIONS = ("cbz", "cbr", "pdf")
_PAGE_SIZE = 100
_MAX_SNAPSHOT_PAGES = 10
_MAX_CLIENT_FANOUT = 4


class _AutomaticSearchDeferredError(Exception):
    """Internal control flow for a bounded, non-queued automatic deferral."""


class AirDcppSearchApi(Protocol):
    async def create_search_instance(
        self,
        *,
        expiration_minutes: int,
        owner_suffix: str,
    ) -> AirDcppSearchInstance: ...

    async def get_search_results(
        self,
        instance_id: int,
        *,
        start: int,
        count: int,
    ) -> list[AirDcppSearchResult]: ...

    async def delete_search_instance(self, instance_id: int) -> None: ...


class AirDcppSearchSocket(Protocol):
    async def subscribe(
        self,
        path: str,
        handler: Callable[[Any], Awaitable[None]],
    ) -> None: ...

    async def unsubscribe(self, path: str) -> None: ...

    async def request(
        self,
        method: str,
        path: str,
        data: object | None = None,
    ) -> object: ...


class AirDcppSearchCooldownProtocol(Protocol):
    async def reserve(self, config_id: int) -> AirDcppCooldownReservation: ...

    async def status(self, config_id: int) -> AirDcppCooldownReservation: ...

    async def extend_from_sent(self, config_id: int) -> datetime: ...


@dataclass(frozen=True, slots=True)
class AirDcppSearchClient:
    """One ready exact-client search operation detached from the database."""

    config_id: int
    client_identity: str
    client_name: str
    client_priority: int
    api_client: AirDcppSearchApi
    socket_client: AirDcppSearchSocket
    manual_collection_seconds: int
    automatic_collection_seconds: int
    max_results: int
    max_retained_routes: int
    search_dispatch_deadline_seconds: float
    hub_allowlist: tuple[str, ...]
    max_concurrent_searches: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrent_searches <= 4:
            raise ValueError("AirDC++ per-client search concurrency must be between 1 and 4")


@dataclass(slots=True)
class _RawRoute:
    release: ReleaseResult
    route: DcRoute
    metrics: DcMetrics
    alternate_routes: tuple[DcRoute, ...] = ()


@dataclass(slots=True)
class _ClientResult:
    routes: list[_RawRoute]
    summary: DcClientSearchSummary
    partial: bool


@dataclass(slots=True)
class _SharedClientSearch:
    task: asyncio.Task[_ClientResult]
    callbacks: dict[int, Callable[[AirDcppSearchProgress], Awaitable[None]]]
    waiters: int = 0


class AirDcppSearchCoordinator:
    """Search ready clients independently and return one bounded DC outcome."""

    def __init__(
        self,
        *,
        cooldown: AirDcppSearchCooldownProtocol,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._cooldown = cooldown
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._client_semaphores: dict[int, tuple[int, asyncio.Semaphore]] = {}
        self._inflight: dict[tuple[object, ...], _SharedClientSearch] = {}
        self._inflight_lock = asyncio.Lock()

    async def cooldown_status(self, config_ids: Sequence[int]) -> dict[int, int]:
        """Read per-client remaining seconds without reserving hub searches."""
        reservations = await asyncio.gather(
            *(self._cooldown.status(config_id) for config_id in config_ids)
        )
        return {reservation.config_id: reservation.wait_seconds for reservation in reservations}

    async def search(
        self,
        clients: Sequence[AirDcppSearchClient],
        target: IssueSearchTarget,
        *,
        manual: bool,
        validator_kwargs: ValidatorKwargs | None = None,
        on_progress: Callable[[AirDcppSearchProgress], Awaitable[None]] | None = None,
    ) -> DcSearchOutcome:
        """Run one query on every eligible exact client with bounded fan-out."""
        started = time.monotonic()
        semaphore = asyncio.Semaphore(_MAX_CLIENT_FANOUT)

        async def run(client: AirDcppSearchClient) -> _ClientResult:
            async with semaphore:
                return await self._coalesced_search_client(
                    client,
                    target,
                    manual=manual,
                    on_progress=on_progress,
                )

        client_results = await asyncio.gather(*(run(client) for client in clients))
        raw_routes = [route for result in client_results for route in result.routes]
        retained, dropped_by_dedupe = _deduplicate_routes(raw_routes)
        validator = ReleaseValidator(**(validator_kwargs or {}))
        releases = [item.release for item in retained]
        matched_validation, rejected_validation = validator.validate_all_results(
            releases,
            wanted_series=target.series_title,
            wanted_issue=target.issue_number,
            wanted_year=target.search_year,
            wanted_issue_type=target.issue_type,
            alternate_names=target.alternate_names,
            wanted_issue_title=target.issue_title,
            wanted_series_issue_count=target.series_issue_count,
        )
        by_release = {id(item.release): item for item in retained}
        matched = tuple(
            _validated(by_release[id(validation.release)], validation)
            for validation in matched_validation
        )
        rejected = tuple(
            _validated(by_release[id(validation.release)], validation)
            for validation in rejected_validation
        )
        raw_count = sum(item.summary.raw_count for item in client_results)
        dropped = sum(item.summary.dropped_count for item in client_results) + dropped_by_dedupe
        return DcSearchOutcome(
            matched=matched,
            rejected=rejected,
            client_summaries=tuple(item.summary for item in client_results),
            raw_count=raw_count,
            deduplicated_count=len(retained),
            dropped_count=dropped,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            partial=any(item.partial for item in client_results),
        )

    def _client_semaphore(self, client: AirDcppSearchClient) -> asyncio.Semaphore:
        current = self._client_semaphores.get(client.config_id)
        if current is None or current[0] != client.max_concurrent_searches:
            semaphore = asyncio.Semaphore(client.max_concurrent_searches)
            self._client_semaphores[client.config_id] = (
                client.max_concurrent_searches,
                semaphore,
            )
            return semaphore
        return current[1]

    async def _coalesced_search_client(
        self,
        client: AirDcppSearchClient,
        target: IssueSearchTarget,
        *,
        manual: bool,
        on_progress: Callable[[AirDcppSearchProgress], Awaitable[None]] | None,
    ) -> _ClientResult:
        collection_seconds = (
            client.manual_collection_seconds if manual else client.automatic_collection_seconds
        )
        key: tuple[object, ...] = (
            client.config_id,
            manual,
            _query_pattern(target),
            client.hub_allowlist,
            collection_seconds,
            client.max_results,
        )
        waiter_token = id(asyncio.current_task())
        async with self._inflight_lock:
            shared = self._inflight.get(key)
            if shared is None:
                callbacks: dict[
                    int,
                    Callable[[AirDcppSearchProgress], Awaitable[None]],
                ] = {}

                async def broadcast(progress: AirDcppSearchProgress) -> None:
                    current_callbacks = tuple(callbacks.values())
                    if current_callbacks:
                        await asyncio.gather(
                            *(callback(progress) for callback in current_callbacks),
                            return_exceptions=True,
                        )

                async def run_limited() -> _ClientResult:
                    async with self._client_semaphore(client):
                        return await self._search_client(
                            client,
                            target,
                            manual=manual,
                            on_progress=broadcast,
                        )

                shared = _SharedClientSearch(
                    task=asyncio.create_task(run_limited()),
                    callbacks=callbacks,
                )
                self._inflight[key] = shared
            shared.waiters += 1
            if on_progress is not None:
                shared.callbacks[waiter_token] = on_progress

        cancel_task = False
        try:
            return await asyncio.shield(shared.task)
        finally:
            async with self._inflight_lock:
                shared.callbacks.pop(waiter_token, None)
                shared.waiters -= 1
                if shared.waiters == 0:
                    self._inflight.pop(key, None)
                    cancel_task = not shared.task.done()
                    if cancel_task:
                        shared.task.cancel()
            if cancel_task:
                await asyncio.gather(shared.task, return_exceptions=True)

    async def _search_client(
        self,
        client: AirDcppSearchClient,
        target: IssueSearchTarget,
        *,
        manual: bool,
        on_progress: Callable[[AirDcppSearchProgress], Awaitable[None]] | None,
    ) -> _ClientResult:
        started = time.monotonic()
        instance: AirDcppSearchInstance | None = None
        listener_paths: list[str] = []
        raw_results: dict[str, AirDcppSearchResult] = {}
        conflicting_results: list[AirDcppSearchResult] = []
        dropped = 0
        partial = False
        status = DcClientSearchStatus.FAILED
        cancelled = False
        sent_event = asyncio.Event()
        sent_count: int | None = None

        async def progress(
            state: AirDcppSearchProgressState,
            remaining_seconds: int | None = None,
        ) -> None:
            if on_progress is not None:
                await on_progress(
                    AirDcppSearchProgress(
                        config_id=client.config_id,
                        client_name=client.client_name,
                        state=state,
                        remaining_seconds=remaining_seconds,
                    )
                )

        async def on_queued(_payload: object) -> None:
            await progress(AirDcppSearchProgressState.QUEUED)

        async def on_sent(payload: object) -> None:
            nonlocal dropped, sent_count
            try:
                event = AirDcppSearchSentEvent.model_validate(payload)
            except ValidationError:
                dropped += 1
                return
            sent_count = event.sent
            await self._cooldown.extend_from_sent(client.config_id)
            sent_event.set()
            await progress(
                AirDcppSearchProgressState.ZERO_HUBS
                if event.sent == 0
                else AirDcppSearchProgressState.COLLECTING
            )

        async def on_result(payload: object) -> None:
            nonlocal dropped
            try:
                event = AirDcppSearchResultEvent.model_validate(payload)
            except ValidationError:
                dropped += 1
                return
            if event.result.id in raw_results:
                raw_results[event.result.id] = event.result
                return
            if len(raw_results) >= client.max_results:
                dropped += 1
                return
            raw_results[event.result.id] = event.result

        try:
            if not await self._wait_for_reservation(client, progress, manual=manual):
                raise _AutomaticSearchDeferredError
            await progress(AirDcppSearchProgressState.STARTING)
            instance = await client.api_client.create_search_instance(
                expiration_minutes=5,
                owner_suffix="pullbox",
            )
            listeners = {
                "search_hub_searches_queued": on_queued,
                "search_hub_searches_sent": on_sent,
                "search_result_added": on_result,
                "search_result_updated": on_result,
            }
            for event_name, handler in listeners.items():
                path = f"/search/{instance.id}/listeners/{event_name}"
                await client.socket_client.subscribe(path, handler)
                listener_paths.append(path)

            payload: dict[str, object] = {
                "query": {
                    "pattern": _query_pattern(target),
                    "file_type": "file",
                    "extensions": list(_SEARCH_EXTENSIONS),
                },
                "priority": 3 if manual else 2,
            }
            if client.hub_allowlist:
                payload["hub_urls"] = list(client.hub_allowlist)
            try:
                async with asyncio.timeout(client.search_dispatch_deadline_seconds):
                    await client.socket_client.request(
                        "POST",
                        f"/search/{instance.id}/hub_search",
                        payload,
                    )
                    await sent_event.wait()
            except Exception:
                if not sent_event.is_set():
                    raise
                # Once AirDC++ confirms hub dispatch, preserve the durable gate
                # and finish from the bounded REST snapshot if the socket drops.
                partial = True

            if sent_count == 0:
                status = DcClientSearchStatus.ZERO_HUBS
            else:
                await self._sleep(
                    client.manual_collection_seconds
                    if manual
                    else client.automatic_collection_seconds
                )
                await progress(AirDcppSearchProgressState.FINISHING)
                try:
                    snapshot_results = await self._final_snapshot(
                        client,
                        instance.id,
                        remaining=max(0, client.max_results - len(raw_results)),
                        seen_result_ids=frozenset(raw_results),
                    )
                    for result in snapshot_results:
                        existing = raw_results.get(result.id)
                        if existing is not None and (
                            existing.tth != result.tth or existing.size != result.size
                        ):
                            conflicting_results.append(result)
                        else:
                            raw_results[result.id] = result
                except Exception:
                    partial = True
                status = DcClientSearchStatus.PARTIAL if partial else DcClientSearchStatus.COMPLETED
                await progress(AirDcppSearchProgressState.COMPLETE)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except _AutomaticSearchDeferredError:
            status = DcClientSearchStatus.DEFERRED_COOLDOWN
        except TimeoutError:
            partial = True
            status = DcClientSearchStatus.DISPATCH_TIMEOUT
            await progress(AirDcppSearchProgressState.FAILED)
        except Exception:
            partial = True
            status = DcClientSearchStatus.UNAVAILABLE
            await progress(AirDcppSearchProgressState.FAILED)
        finally:
            for path in reversed(listener_paths):
                with suppress(Exception):
                    await client.socket_client.unsubscribe(path)
            # Successful result routes remain backed by AirDC++ until its
            # bounded search-instance expiry. Empty and abandoned searches can
            # be reclaimed immediately.
            if instance is not None and (cancelled or not raw_results):
                with suppress(Exception):
                    await client.api_client.delete_search_instance(instance.id)

        routes: list[_RawRoute] = []
        if instance is not None:
            for result in (*raw_results.values(), *conflicting_results):
                normalized = _normalize_result(client, instance, result, now=self._now())
                if normalized is None:
                    dropped += 1
                else:
                    routes.append(normalized)
        per_client, dedupe_dropped = _deduplicate_routes(routes)
        dropped += dedupe_dropped
        summary = DcClientSearchSummary(
            client_config_id=client.config_id,
            client_identity=client.client_identity,
            client_name=client.client_name,
            status=status,
            raw_count=len(raw_results) + len(conflicting_results),
            retained_count=len(per_client),
            dropped_count=dropped,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return _ClientResult(routes=per_client, summary=summary, partial=partial)

    async def _wait_for_reservation(
        self,
        client: AirDcppSearchClient,
        progress: Callable[[AirDcppSearchProgressState, int | None], Awaitable[None]],
        *,
        manual: bool,
    ) -> bool:
        while True:
            reservation = await self._cooldown.reserve(client.config_id)
            if reservation.granted:
                return True
            if not manual:
                return False
            for remaining in range(reservation.wait_seconds, 0, -1):
                await progress(AirDcppSearchProgressState.COOLDOWN, remaining)
                await self._sleep(1)

    async def _final_snapshot(
        self,
        client: AirDcppSearchClient,
        instance_id: int,
        *,
        remaining: int,
        seen_result_ids: frozenset[str],
    ) -> list[AirDcppSearchResult]:
        if remaining <= 0:
            return []
        results: list[AirDcppSearchResult] = []
        retained_ids = set(seen_result_ids)
        start = 0
        for _page_index in range(_MAX_SNAPSHOT_PAGES):
            page = await client.api_client.get_search_results(
                instance_id,
                start=start,
                count=_PAGE_SIZE,
            )
            for result in page:
                results.append(result)
                if result.id not in retained_ids:
                    retained_ids.add(result.id)
                    remaining -= 1
                    if remaining == 0:
                        break
            if remaining == 0 or len(page) < _PAGE_SIZE:
                break
            start += len(page)
        return results


def _query_pattern(target: IssueSearchTarget) -> str:
    number = format_issue_number(target.issue_number)
    return f"{target.series_title} {number}"


def _normalize_result(
    client: AirDcppSearchClient,
    instance: AirDcppSearchInstance,
    result: AirDcppSearchResult,
    *,
    now: datetime,
) -> _RawRoute | None:
    if (
        not result.file_result
        or result.tth is None
        or result.size <= 0
        or PurePath(result.name).suffix.casefold().lstrip(".") not in _SEARCH_EXTENSIONS
    ):
        return None
    expires_in = max(1, min(instance.expires_in // 1000, 600))
    route = DcRoute(
        client_config_id=client.config_id,
        client_identity=client.client_identity,
        search_instance_id=instance.id,
        grouped_result_id=result.id,
        result_expires_at=now.astimezone(UTC) + timedelta(seconds=expires_in),
        tth=result.tth,
        size_bytes=result.size,
    )
    metrics = DcMetrics(
        source_count=result.users.count,
        free_slots=result.slots.free,
        total_slots=result.slots.total,
        aggregate_connection_bytes_per_second=result.connection,
    )
    release = ReleaseResult(
        title=result.name,
        indexer_name=client.client_name,
        indexer_id=None,
        download_url=f"airdcpp://client/{client.config_id}/tth/{result.tth}",
        size_bytes=result.size,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        category=None,
        published_at=None,
        ranking_priority=client.client_priority,
        protocol=AcquisitionProtocol.DC,
    )
    return _RawRoute(release=release, route=route, metrics=metrics)


def _deduplicate_routes(routes: Sequence[_RawRoute]) -> tuple[list[_RawRoute], int]:
    sizes_by_tth: dict[str, set[int]] = {}
    for item in routes:
        sizes_by_tth.setdefault(item.route.tth, set()).add(item.route.size_bytes)
    conflicting_tths = {tth for tth, sizes in sizes_by_tth.items() if len(sizes) > 1}
    grouped: dict[tuple[str, int], list[_RawRoute]] = {}
    dropped = 0
    for item in routes:
        if item.route.tth in conflicting_tths:
            dropped += 1
            continue
        grouped.setdefault((item.route.tth, item.route.size_bytes), []).append(item)
    retained: list[_RawRoute] = []
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                item.release.ranking_priority,
                0 if item.metrics.free_slots > 0 else 1,
                -item.metrics.free_slots,
                -item.metrics.source_count,
                -(item.metrics.aggregate_connection_bytes_per_second or 0),
                item.route.client_config_id,
                item.route.grouped_result_id,
            )
        )
        alternate_routes = tuple(item.route for item in items[1:21])
        primary = replace(items[0], alternate_routes=alternate_routes)
        retained.append(primary)
        dropped += max(0, len(items) - 21)
    retained.sort(key=lambda item: (item.release.ranking_priority, item.release.title.casefold()))
    return retained, dropped


def _validated(raw: _RawRoute, validation: Any) -> DcValidatedCandidate:
    return DcValidatedCandidate(
        release=raw.release,
        validation=validation,
        route=raw.route,
        metrics=raw.metrics,
        alternate_routes=raw.alternate_routes,
    )
