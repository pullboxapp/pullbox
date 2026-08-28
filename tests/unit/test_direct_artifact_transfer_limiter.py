from __future__ import annotations

import asyncio

import pytest

from pullbox.models.direct_acquisition import DirectArtifactHostKind
from pullbox.providers.artifact_hosts.limiter import (
    ArtifactTransferLimiter,
    DirectProviderTransferLimiter,
)
from pullbox.providers.artifact_hosts.transport_contract import (
    ArtifactTransferCancelledError,
)


@pytest.mark.asyncio
async def test_host_waiter_does_not_consume_global_slot_or_block_other_host() -> None:
    limiter = ArtifactTransferLimiter(global_limit=2, per_host_limit=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    unrelated_started = asyncio.Event()

    async def run_first() -> None:
        async with limiter.slot(DirectArtifactHostKind.PIXELDRAIN):
            first_started.set()
            await release_first.wait()

    async def run_second() -> None:
        async with limiter.slot(DirectArtifactHostKind.PIXELDRAIN):
            second_started.set()

    async def run_unrelated() -> None:
        async with limiter.slot(DirectArtifactHostKind.MEGA):
            unrelated_started.set()

    first = asyncio.create_task(run_first())
    await first_started.wait()
    second = asyncio.create_task(run_second())
    unrelated = asyncio.create_task(run_unrelated())

    await asyncio.wait_for(unrelated_started.wait(), timeout=1)
    assert not second_started.is_set()

    release_first.set()
    await asyncio.gather(first, second, unrelated)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_limiter_honors_host_override_and_global_bound() -> None:
    limiter = ArtifactTransferLimiter(
        global_limit=2,
        per_host_limit=1,
        host_limits={DirectArtifactHostKind.PIXELDRAIN: 2},
    )
    active = 0
    maximum = 0
    release = asyncio.Event()
    both_started = asyncio.Event()

    async def run() -> None:
        nonlocal active, maximum
        async with limiter.slot(DirectArtifactHostKind.PIXELDRAIN):
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(run()) for _ in range(3)]
    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert maximum == 2
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cancelled_host_waiter_does_not_wait_for_active_transfer() -> None:
    limiter = ArtifactTransferLimiter(global_limit=2, per_host_limit=1)
    owner_started = asyncio.Event()
    release_owner = asyncio.Event()
    cancel_waiter = asyncio.Event()
    waiter_entered = False

    async def run_owner() -> None:
        async with limiter.slot(DirectArtifactHostKind.PIXELDRAIN):
            owner_started.set()
            await release_owner.wait()

    async def run_waiter() -> None:
        nonlocal waiter_entered
        async with limiter.slot(
            DirectArtifactHostKind.PIXELDRAIN,
            cancel_event=cancel_waiter,
        ):
            waiter_entered = True

    owner = asyncio.create_task(run_owner())
    await owner_started.wait()
    waiter = asyncio.create_task(run_waiter())
    await asyncio.sleep(0)

    cancel_waiter.set()
    with pytest.raises(ArtifactTransferCancelledError):
        await asyncio.wait_for(waiter, timeout=0.25)
    assert waiter_entered is False

    release_owner.set()
    await owner

    # Cancellation must not leak the host permit.
    async with limiter.slot(DirectArtifactHostKind.PIXELDRAIN):
        pass


@pytest.mark.asyncio
async def test_provider_limiter_serializes_each_provider_without_blocking_others() -> None:
    limiter = DirectProviderTransferLimiter(per_provider_limit=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    same_provider_started = asyncio.Event()
    other_provider_started = asyncio.Event()

    async def run_first() -> None:
        async with limiter.slot("pullbox.libgen"):
            first_started.set()
            await release_first.wait()

    async def run_same_provider() -> None:
        async with limiter.slot("pullbox.libgen"):
            same_provider_started.set()

    async def run_other_provider() -> None:
        async with limiter.slot("pullbox.getcomics"):
            other_provider_started.set()

    first = asyncio.create_task(run_first())
    await first_started.wait()
    same_provider = asyncio.create_task(run_same_provider())
    other_provider = asyncio.create_task(run_other_provider())

    await asyncio.wait_for(other_provider_started.wait(), timeout=1)
    assert same_provider_started.is_set() is False

    release_first.set()
    await asyncio.gather(first, same_provider, other_provider)
    assert same_provider_started.is_set() is True


@pytest.mark.asyncio
async def test_cancelled_provider_waiter_does_not_leak_provider_permit() -> None:
    limiter = DirectProviderTransferLimiter(per_provider_limit=1)
    owner_started = asyncio.Event()
    release_owner = asyncio.Event()
    cancel_waiter = asyncio.Event()
    waiter_entered = False

    async def run_owner() -> None:
        async with limiter.slot("pullbox.libgen"):
            owner_started.set()
            await release_owner.wait()

    async def run_waiter() -> None:
        nonlocal waiter_entered
        async with limiter.slot("pullbox.libgen", cancel_event=cancel_waiter):
            waiter_entered = True

    owner = asyncio.create_task(run_owner())
    await owner_started.wait()
    waiter = asyncio.create_task(run_waiter())
    await asyncio.sleep(0)

    cancel_waiter.set()
    with pytest.raises(ArtifactTransferCancelledError):
        await asyncio.wait_for(waiter, timeout=0.25)
    assert waiter_entered is False

    release_owner.set()
    await owner
    async with limiter.slot("pullbox.libgen"):
        pass


def test_limiter_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="Global"):
        ArtifactTransferLimiter(global_limit=0)
    with pytest.raises(ValueError, match="host"):
        ArtifactTransferLimiter(global_limit=2, per_host_limit=0)
    with pytest.raises(ValueError, match="provider"):
        DirectProviderTransferLimiter(per_provider_limit=0)
