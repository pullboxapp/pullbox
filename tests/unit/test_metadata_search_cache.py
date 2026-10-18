"""Provider-only snapshots retain outcomes, isolate callers and bound resources."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.schemas.metadata_sources import (
    MetadataDomain,
    ProviderSeriesRead,
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SourceOutcome,
    SourceStatus,
)
from pullbox.services.metadata_search_cache import (
    MetadataSearchBusyError,
    MetadataSearchCache,
    discovery_cache_key,
)
from pullbox.services.metadata_sources import SourceRuntime, default_policy


def result(status=SourceStatus.OK, *, rows=True):
    source = Source.METRON_API
    return SeriesDiscoveryRead(
        results=[
            ProviderSeriesRead(
                source=source,
                identity_namespace=source.identity_namespace,
                external_id="42",
                title="Example",
            )
        ]
        if rows
        else [],
        sources=[
            SourceOutcome(source=source, status=status, truncated=status is not SourceStatus.OK)
        ],
    )


async def test_snapshot_reuse_does_not_share_mutable_results_or_outcomes():
    load = AsyncMock(return_value=result())
    cache = MetadataSearchCache()
    first = await cache.get("key", load)
    first.results[0].title = "Changed by one view"
    first.sources[0].status = SourceStatus.UNAVAILABLE
    second = await cache.get("key", load)
    assert load.await_count == 1
    assert second.results[0].title == "Example"
    assert second.sources[0].status is SourceStatus.OK


async def test_metron_retry_message_is_not_frozen_in_a_search_snapshot():
    paused = result(SourceStatus.RATE_LIMITED, rows=False)
    paused.sources[0].retry_after_seconds = 3
    load = AsyncMock(side_effect=[paused, result()])
    cache = MetadataSearchCache()
    await cache.get("key", load)
    assert (await cache.get("key", load)).sources[0].status is SourceStatus.OK
    assert load.await_count == 2


@pytest.mark.parametrize(
    "status,rows,ttl",
    [
        (SourceStatus.OK, True, 300),
        (SourceStatus.EMPTY, False, 30),
        (SourceStatus.TIMEOUT, True, 30),
        (SourceStatus.UNAVAILABLE, False, 30),
    ],
)
async def test_expiry_keeps_partial_failures_honest_and_short_lived(status, rows, ttl):
    now = [0.0]
    load = AsyncMock(return_value=result(status, rows=rows))
    cache = MetadataSearchCache(clock=lambda: now[0])
    await cache.get("key", load)
    now[0] = ttl - 1
    cached = await cache.get("key", load)
    assert cached.sources[0].status is status
    assert load.await_count == 1
    now[0] = ttl + 1
    await cache.get("key", load)
    assert load.await_count == 2


async def test_identical_waiters_share_work_but_cancelling_one_does_not_cancel_other():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def load():
        calls.append(1)
        started.set()
        await release.wait()
        return result()

    cache = MetadataSearchCache()
    first = asyncio.create_task(cache.get("key", load))
    await started.wait()
    second = asyncio.create_task(cache.get("key", load))
    await asyncio.sleep(0)
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).results[0].title == "Example"
        assert calls == [1]
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_last_cancelled_waiter_drains_work_and_does_not_cache_partial_success():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    cache = MetadataSearchCache()
    task = asyncio.create_task(cache.get("key", load))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    replacement = AsyncMock(return_value=result())
    assert (await cache.get("key", replacement)).results
    assert replacement.await_count == 1


async def test_different_searches_are_admission_bounded():
    started, release = asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        await release.wait()
        return result()

    cache = MetadataSearchCache(max_pending=1)
    task = asyncio.create_task(cache.get("first", load))
    await started.wait()
    second = AsyncMock(return_value=result())
    try:
        with pytest.raises(MetadataSearchBusyError):
            await cache.get("second", second)
        second.assert_not_awaited()
    finally:
        release.set()
        await task


async def test_arrival_during_cancel_cleanup_does_not_join_dying_search():
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    cache = MetadataSearchCache(max_pending=1)
    first = asyncio.create_task(cache.get("key", load))
    await started.wait()
    first.cancel()
    await draining.wait()
    replacement = AsyncMock(return_value=result())
    try:
        with pytest.raises(MetadataSearchBusyError):
            await asyncio.wait_for(cache.get("key", replacement), timeout=0.2)
        replacement.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert (await cache.get("key", replacement)).results
    assert replacement.await_count == 1


async def test_lru_entry_and_byte_limits_evict_old_snapshots():
    load = AsyncMock(return_value=result())
    cache = MetadataSearchCache(max_entries=2)
    for key in ["first", "second", "first", "third", "first", "second"]:
        await cache.get(key, load)
    assert load.await_count == 4
    load.reset_mock()
    size = len(result().model_dump_json().encode())
    cache = MetadataSearchCache(max_bytes=size + 1)
    for key in ["first", "second", "first"]:
        await cache.get(key, load)
    assert load.await_count == 3


async def test_oversized_result_and_raised_exception_are_not_cached():
    load = AsyncMock(return_value=result())
    cache = MetadataSearchCache(max_bytes=1)
    await cache.get("key", load)
    await cache.get("key", load)
    assert load.await_count == 2
    failed = AsyncMock(side_effect=ValueError("provider failure"))
    with pytest.raises(ValueError):
        await cache.get("failed", failed)
    assert (await cache.get("failed", load)).results


async def test_uncacheable_load_keeps_admission_without_reusing_or_storing_results():
    cache = MetadataSearchCache()
    load = AsyncMock(return_value=result())
    await cache.get("key", load, cache_result=False)
    await cache.get("key", load, cache_result=False)
    assert load.await_count == 2
    await cache.get("key", load)
    assert load.await_count == 3


def test_key_is_normalized_opaque_and_bound_to_source_configuration_and_generation():
    source = Source.METRON_API
    runtime = [SourceRuntime(default_policy(source), SecretStr("synthetic-private-token"))]
    query = SeriesDiscoveryQuery(query="  Test    Series ", sources=[source])

    def key(q=query, r=runtime, generation="one", flag=False):
        return discovery_cache_key(q, r, catalog_generation=generation, gcd_api_enabled=flag)

    original = key()
    assert len(original) == 64
    assert "Test" not in original and "synthetic" not in original
    assert key(query.model_copy(update={"query": "test series"})) == original
    assert key(query.model_copy(update={"year": 2000})) != original
    assert key(query.model_copy(update={"search_mode": "full"})) != original
    assert key(generation="two") != original
    assert key(flag=True) != original
    revised = SourceRuntime(runtime[0].policy.model_copy(update={"revision": 2}))
    assert key(r=[revised]) != original
    enabled = SourceRuntime(runtime[0].policy.model_copy(update={"enabled": True}))
    assert key(r=[enabled]) != original
    priority = SourceRuntime(
        runtime[0].policy.model_copy(update={"domain_priorities": {MetadataDomain.CORE: 1}})
    )
    assert key(r=[priority]) != original
