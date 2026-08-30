"""Bounded AirDC++ manual-search lifecycle contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pullbox.models.issue import IssueType
from pullbox.providers.airdcpp.contracts import AirDcppSearchInstance, AirDcppSearchResult
from pullbox.services.airdcpp_search_cooldown import AirDcppCooldownReservation
from pullbox.services.airdcpp_search_coordinator import (
    AirDcppSearchClient,
    AirDcppSearchCoordinator,
    _query_pattern,
)
from pullbox.services.airdcpp_search_types import (
    AirDcppSearchProgressState,
    DcClientSearchStatus,
)
from pullbox.services.search_targets import IssueSearchOutcome, IssueSearchTarget

_TTH = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"
_SECOND_TTH = "B" * 39


def test_issue_search_outcome_has_typed_direct_connect_lane() -> None:
    assert "dc_outcome" in IssueSearchOutcome.__dataclass_fields__


def _target() -> IssueSearchTarget:
    return IssueSearchTarget(
        issue_id=1,
        series_id=2,
        series_title="Example Comic",
        issue_number=1.0,
        issue_type=IssueType.ISSUE,
        series_year=2026,
    )


def test_large_issue_query_pattern_never_uses_scientific_notation() -> None:
    assert _query_pattern(replace(_target(), issue_number=1_000_000.0)) == "Example Comic 1000000"


def _instance() -> AirDcppSearchInstance:
    return AirDcppSearchInstance.model_validate(
        {
            "id": 44,
            "expires_in": 60_000,
            "current_search_id": 0,
            "owner": "session:123:pullbox",
            "queue_time": 0,
            "queued_count": 0,
            "result_count": 1,
            "searches_sent_ago": 0,
        }
    )


def _result(
    *,
    free: int = 1,
    result_id: str = _TTH,
    tth: str = _TTH,
    size: int = 100_000_000,
) -> AirDcppSearchResult:
    return AirDcppSearchResult.model_validate(
        {
            "id": result_id,
            "name": "Example Comic 001 (2026).cbz",
            "relevance": 1.0,
            "hits": 2,
            "users": {"count": 2},
            "type": {"id": "file"},
            "path": "/private/peer/path",
            "tth": tth,
            "time": 0,
            "slots": {"free": free, "total": 2, "str": f"{free}/2"},
            "connection": 1_000_000,
            "size": size,
        }
    )


class _FakeApi:
    def __init__(self, results: list[AirDcppSearchResult] | None = None) -> None:
        self.results = results or []
        self.created = 0
        self.deleted: list[int] = []
        self.pages: list[tuple[int, int, int]] = []

    async def create_search_instance(self, **_kwargs: object) -> AirDcppSearchInstance:
        self.created += 1
        return _instance()

    async def get_search_results(
        self,
        instance_id: int,
        *,
        start: int,
        count: int,
    ) -> list[AirDcppSearchResult]:
        self.pages.append((instance_id, start, count))
        return self.results[start : start + count]

    async def delete_search_instance(self, instance_id: int) -> None:
        self.deleted.append(instance_id)


class _FakeSocket:
    def __init__(
        self,
        *,
        sent: int = 1,
        emit_result: bool = True,
        emit_update: bool = False,
        block_send: bool = False,
        fail_after_sent: bool = False,
    ) -> None:
        self.sent = sent
        self.emit_result = emit_result
        self.emit_update = emit_update
        self.block_send = block_send
        self.fail_after_sent = fail_after_sent
        self.handlers: dict[str, Any] = {}
        self.calls: list[tuple[str, str, object | None]] = []
        self.unsubscribed: list[str] = []
        self.send_started = asyncio.Event()

    async def subscribe(self, path: str, handler: Any) -> None:
        self.calls.append(("SUBSCRIBE", path, None))
        self.handlers[path] = handler

    async def unsubscribe(self, path: str) -> None:
        self.calls.append(("UNSUBSCRIBE", path, None))
        self.unsubscribed.append(path)
        self.handlers.pop(path, None)

    async def request(self, method: str, path: str, data: object | None = None) -> object:
        self.calls.append((method, path, data))
        assert all(
            listener in self.handlers
            for listener in (
                "/search/44/listeners/search_hub_searches_queued",
                "/search/44/listeners/search_hub_searches_sent",
                "/search/44/listeners/search_result_added",
                "/search/44/listeners/search_result_updated",
            )
        )
        self.send_started.set()
        if self.block_send:
            await asyncio.Event().wait()
        await self.handlers["/search/44/listeners/search_hub_searches_sent"](
            {
                "sent": self.sent,
                "search_id": "active-search-id",
                "query": {"pattern": "Example Comic 1"},
            }
        )
        if self.emit_result:
            result = _result(free=0).model_dump()
            await self.handlers["/search/44/listeners/search_result_added"](
                {"result": result, "search_id": "active-search-id"}
            )
            if self.emit_update:
                await self.handlers["/search/44/listeners/search_result_updated"](
                    {"result": result, "search_id": "active-search-id"}
                )
        if self.fail_after_sent:
            raise ConnectionError("socket disconnected")
        return {"queued_count": 0}


class _FakeCooldown:
    def __init__(self, reservations: list[AirDcppCooldownReservation] | None = None) -> None:
        now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        self.reservations = reservations or [
            AirDcppCooldownReservation(7, True, now, now + timedelta(seconds=45), 0)
        ]
        self.reserve_calls = 0
        self.extended = 0

    async def reserve(self, _config_id: int) -> AirDcppCooldownReservation:
        result = self.reservations[min(self.reserve_calls, len(self.reservations) - 1)]
        self.reserve_calls += 1
        return result

    async def extend_from_sent(self, _config_id: int) -> datetime:
        self.extended += 1
        return datetime(2026, 8, 25, 12, 0, 45, tzinfo=UTC)

    async def status(self, config_id: int) -> AirDcppCooldownReservation:
        result = self.reservations[min(config_id - 7, len(self.reservations) - 1)]
        return result


def _client(
    api: _FakeApi,
    socket: _FakeSocket,
    *,
    config_id: int = 7,
    client_priority: int = 20,
    max_results: int = 200,
) -> AirDcppSearchClient:
    return AirDcppSearchClient(
        config_id=config_id,
        client_identity=f"airdcpp:{config_id}",
        client_name=f"Dedicated Air {config_id}",
        client_priority=client_priority,
        api_client=api,
        socket_client=socket,
        manual_collection_seconds=1,
        automatic_collection_seconds=2,
        max_results=max_results,
        max_retained_routes=400,
        search_dispatch_deadline_seconds=5,
        hub_allowlist=(),
    )


@pytest.mark.asyncio
async def test_search_subscribes_before_send_extends_from_event_and_deduplicates_snapshot() -> None:
    api = _FakeApi([_result(free=1)])
    socket = _FakeSocket()
    cooldown = _FakeCooldown()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    coordinator = AirDcppSearchCoordinator(cooldown=cooldown, sleep=sleep)
    outcome = await coordinator.search((_client(api, socket),), _target(), manual=True)

    assert socket.calls[0][0] == "SUBSCRIBE"
    assert next(index for index, call in enumerate(socket.calls) if call[0] == "POST") == 4
    request = next(call for call in socket.calls if call[0] == "POST")
    assert request[1] == "/search/44/hub_search"
    assert request[2] == {
        "query": {
            "pattern": "Example Comic 1",
            "file_type": "file",
            "extensions": ["cbz", "cbr", "pdf"],
        },
        "priority": 3,
    }
    assert cooldown.extended == 1
    assert sleeps == [1]
    assert outcome.raw_count == 1
    assert outcome.deduplicated_count == 1
    assert len(outcome.matched) == 1
    assert outcome.matched[0].route.tth == _TTH
    assert outcome.matched[0].metrics.free_slots == 1
    assert outcome.client_summaries[0].status is DcClientSearchStatus.COMPLETED
    assert api.deleted == []
    assert len(socket.unsubscribed) == 4


@pytest.mark.asyncio
async def test_socket_updates_do_not_consume_capacity_for_distinct_snapshot_results() -> None:
    snapshot_result = _result(
        result_id="second-result",
        tth=_SECOND_TTH,
        size=200_000_000,
    )
    api = _FakeApi([snapshot_result])
    socket = _FakeSocket(emit_update=True)
    coordinator = AirDcppSearchCoordinator(
        cooldown=_FakeCooldown(),
        sleep=lambda _s: asyncio.sleep(0),
    )

    outcome = await coordinator.search(
        (_client(api, socket, max_results=2),),
        _target(),
        manual=True,
    )

    assert {candidate.route.tth for candidate in outcome.matched} == {_TTH, _SECOND_TTH}
    assert outcome.raw_count == 2


@pytest.mark.asyncio
async def test_active_cooldown_counts_down_then_reserves_before_mutation() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = _FakeCooldown(
        [
            AirDcppCooldownReservation(7, False, now + timedelta(seconds=3), now, 3),
            AirDcppCooldownReservation(7, True, now, now + timedelta(seconds=45), 0),
        ]
    )
    progress: list[tuple[AirDcppSearchProgressState, int | None]] = []

    async def on_progress(event) -> None:
        progress.append((event.state, event.remaining_seconds))

    coordinator = AirDcppSearchCoordinator(cooldown=cooldown, sleep=lambda _s: asyncio.sleep(0))
    await coordinator.search(
        (_client(_FakeApi(), _FakeSocket(emit_result=False)),),
        _target(),
        manual=True,
        on_progress=on_progress,
    )

    assert progress[:3] == [
        (AirDcppSearchProgressState.COOLDOWN, 3),
        (AirDcppSearchProgressState.COOLDOWN, 2),
        (AirDcppSearchProgressState.COOLDOWN, 1),
    ]
    assert (AirDcppSearchProgressState.STARTING, None) in progress
    assert cooldown.reserve_calls == 2


@pytest.mark.asyncio
async def test_status_reports_longest_exact_client_wait_without_reserving() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = _FakeCooldown(
        [
            AirDcppCooldownReservation(7, True, now, now, 0),
            AirDcppCooldownReservation(8, False, now, now + timedelta(seconds=12), 12),
        ]
    )
    coordinator = AirDcppSearchCoordinator(cooldown=cooldown)

    status = await coordinator.cooldown_status((7, 8))

    assert status == {7: 0, 8: 12}
    assert cooldown.reserve_calls == 0


@pytest.mark.asyncio
async def test_zero_hubs_and_cancellation_both_cleanup_without_bypassing_gate() -> None:
    zero_api = _FakeApi()
    zero_socket = _FakeSocket(sent=0, emit_result=False)
    cooldown = _FakeCooldown()
    coordinator = AirDcppSearchCoordinator(cooldown=cooldown, sleep=lambda _s: asyncio.sleep(0))

    zero = await coordinator.search((_client(zero_api, zero_socket),), _target(), manual=True)
    assert zero.client_summaries[0].status is DcClientSearchStatus.ZERO_HUBS
    assert zero.matched == ()
    assert cooldown.extended == 1
    assert zero_api.deleted == [44]

    cancel_api = _FakeApi()
    cancel_socket = _FakeSocket(block_send=True)
    cancel_cooldown = _FakeCooldown()
    task = asyncio.create_task(
        AirDcppSearchCoordinator(
            cooldown=cancel_cooldown,
            sleep=lambda _s: asyncio.sleep(0),
        ).search((_client(cancel_api, cancel_socket),), _target(), manual=True)
    )
    await asyncio.wait_for(cancel_socket.send_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancel_cooldown.reserve_calls == 1
    assert cancel_api.deleted == [44]
    assert len(cancel_socket.unsubscribed) == 4


@pytest.mark.asyncio
async def test_same_tth_with_conflicting_sizes_is_dropped_as_invalid_identity() -> None:
    api = _FakeApi([_result(size=100_000_001)])
    socket = _FakeSocket()
    coordinator = AirDcppSearchCoordinator(
        cooldown=_FakeCooldown(),
        sleep=lambda _s: asyncio.sleep(0),
    )

    outcome = await coordinator.search((_client(api, socket),), _target(), manual=True)

    assert outcome.matched == ()
    assert outcome.rejected == ()
    assert outcome.deduplicated_count == 0
    assert outcome.dropped_count == 2


@pytest.mark.asyncio
async def test_socket_loss_after_sent_uses_bounded_final_rest_snapshot() -> None:
    api = _FakeApi([_result()])
    socket = _FakeSocket(emit_result=False, fail_after_sent=True)
    coordinator = AirDcppSearchCoordinator(
        cooldown=_FakeCooldown(),
        sleep=lambda _s: asyncio.sleep(0),
    )

    outcome = await coordinator.search((_client(api, socket),), _target(), manual=True)

    assert len(outcome.matched) == 1
    assert outcome.partial is True
    assert outcome.client_summaries[0].status is DcClientSearchStatus.PARTIAL
    assert api.pages == [(44, 0, 100)]
    assert api.deleted == []


@pytest.mark.asyncio
async def test_automatic_search_defers_once_during_cooldown_without_queueing() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = _FakeCooldown(
        [AirDcppCooldownReservation(7, False, now, now + timedelta(seconds=45), 45)]
    )
    socket = _FakeSocket()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    outcome = await AirDcppSearchCoordinator(cooldown=cooldown, sleep=sleep).search(
        (_client(_FakeApi(), socket),),
        _target(),
        manual=False,
    )

    assert outcome.client_summaries[0].status is DcClientSearchStatus.DEFERRED_COOLDOWN
    assert cooldown.reserve_calls == 1
    assert socket.calls == []
    assert sleeps == []


@pytest.mark.asyncio
async def test_automatic_search_uses_low_airdcpp_priority() -> None:
    socket = _FakeSocket(emit_result=False)
    await AirDcppSearchCoordinator(
        cooldown=_FakeCooldown(),
        sleep=lambda _s: asyncio.sleep(0),
    ).search((_client(_FakeApi(), socket),), _target(), manual=False)

    request = next(call for call in socket.calls if call[0] == "POST")
    assert request[2]["priority"] == 2


@pytest.mark.asyncio
async def test_identical_concurrent_manual_queries_share_one_remote_search() -> None:
    api = _FakeApi([_result()])
    socket = _FakeSocket(emit_result=False)
    cooldown = _FakeCooldown()
    collecting = asyncio.Event()
    release_collection = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        collecting.set()
        await release_collection.wait()

    coordinator = AirDcppSearchCoordinator(cooldown=cooldown, sleep=sleep)
    first = asyncio.create_task(coordinator.search((_client(api, socket),), _target(), manual=True))
    await asyncio.wait_for(collecting.wait(), timeout=1)
    second = asyncio.create_task(
        coordinator.search((_client(api, socket),), _target(), manual=True)
    )
    await asyncio.sleep(0)
    release_collection.set()
    first_outcome, second_outcome = await asyncio.gather(first, second)

    assert api.created == 1
    assert cooldown.reserve_calls == 1
    assert len(first_outcome.matched) == len(second_outcome.matched) == 1


@pytest.mark.asyncio
async def test_cross_client_route_prefers_free_slot_and_retains_stable_fallback() -> None:
    primary_api = _FakeApi([_result(free=1)])
    fallback_api = _FakeApi([_result(free=0)])
    primary = _client(
        primary_api,
        _FakeSocket(emit_result=False),
        config_id=8,
        client_priority=20,
    )
    fallback = _client(
        fallback_api,
        _FakeSocket(emit_result=False),
        config_id=7,
        client_priority=20,
    )
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cooldown = _FakeCooldown(
        [
            AirDcppCooldownReservation(7, True, now, now + timedelta(seconds=45), 0),
            AirDcppCooldownReservation(8, True, now, now + timedelta(seconds=45), 0),
        ]
    )

    outcome = await AirDcppSearchCoordinator(
        cooldown=cooldown,
        sleep=lambda _s: asyncio.sleep(0),
    ).search((fallback, primary), _target(), manual=True)

    assert len(outcome.matched) == 1
    candidate = outcome.matched[0]
    assert candidate.route.client_config_id == 8
    assert [route.client_config_id for route in candidate.alternate_routes] == [7]
