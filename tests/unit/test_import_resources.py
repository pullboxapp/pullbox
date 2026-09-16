"""Container-aware import budgets and bounded thread scheduling."""

import asyncio
import threading

import pytest

from pullbox.core.import_resources import (
    ImportResources,
    bounded_thread_map,
    detect_import_resources,
)


def test_budget_reserves_memory_and_cpu_for_interactive_work():
    resources = ImportResources(cpu_count=16, available_memory_bytes=16 * 1024**3)
    assert resources.inspection_workers() == 4
    assert resources.inspection_workers(requested=2) == 2
    assert resources.inspection_workers(requested=32) == 12
    assert ImportResources(24, 800 * 1024**2).inspection_workers() == 1
    assert ImportResources(1, 64 * 1024**3).inspection_workers() == 1


def test_cgroup_limits_and_parent_limits_override_host_hardware(tmp_path, monkeypatch):
    import pullbox.core.import_resources as module

    monkeypatch.setattr(module, "_visible_cpus", lambda: 32)
    monkeypatch.setattr(module, "_available_memory", lambda: 64 * 1024**3)
    group = tmp_path / "workload"
    group.mkdir()
    (group / "cpu.max").write_text("max 100000")
    (group / "memory.max").write_text("max")
    (tmp_path / "cpu.max").write_text("250000 100000")
    (tmp_path / "memory.max").write_text(str(4 * 1024**3))
    (tmp_path / "memory.current").write_text(str(3 * 1024**3))
    resources = detect_import_resources(cgroup_path=group, cgroup_root=tmp_path)
    assert resources.cpu_count == 2
    assert resources.available_memory_bytes == 1024**3
    assert resources.inspection_workers() == 1


def test_missing_resource_information_uses_serial_fallback(tmp_path, monkeypatch):
    import pullbox.core.import_resources as module

    monkeypatch.setattr(module, "_visible_cpus", lambda: 1)
    monkeypatch.setattr(module, "_available_memory", lambda: 0)
    assert detect_import_resources(cgroup_path=tmp_path).inspection_workers() == 1


async def test_bounded_thread_map_runs_concurrently_without_eager_submission():
    barrier = threading.Barrier(2)
    started = []
    consumed = []

    def work(value):
        started.append(value)
        if value < 2:
            barrier.wait(timeout=2)
        return value * 2

    async with bounded_thread_map(work, range(20), workers=2) as results:
        async for value in results:
            consumed.append(value)
            assert len(started) <= len(consumed) + 2
    assert sorted(consumed) == list(range(0, 40, 2))


async def test_early_exit_drains_workers_without_submitting_more():
    started = []
    finished = []
    barrier = threading.Barrier(2)

    def work(value):
        started.append(value)
        barrier.wait(timeout=2)
        finished.append(value)
        return value

    with pytest.raises(RuntimeError, match="cancel"):
        async with bounded_thread_map(work, range(100), workers=2) as results:
            async for _ in results:
                raise RuntimeError("cancel")
    assert sorted(started) == sorted(finished) == [0, 1]


async def test_task_cancellation_waits_for_running_thread():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work(value):
        started.set()
        release.wait(timeout=3)
        finished.set()
        return value

    async def consume():
        async with bounded_thread_map(work, [1, 2], workers=1) as results:
            async for _ in results:
                pass

    task = asyncio.create_task(consume())
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_async_pool_drains_active_work_when_consumer_fails():
    from pullbox.core.import_resources import bounded_async_map

    started = []
    finished = []
    both_started = asyncio.Event()

    async def work(value):
        started.append(value)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        finished.append(value)
        return value

    with pytest.raises(RuntimeError, match="stop"):
        async with bounded_async_map(work, range(1000), workers=2) as results:
            async for _ in results:
                raise RuntimeError("stop")
    assert sorted(started) == sorted(finished) == [0, 1]


def test_v1_limits_and_memory_pressure_are_conservative(tmp_path, monkeypatch):
    import pullbox.core.import_resources as module

    monkeypatch.setattr(module, "_visible_cpus", lambda: 24)
    monkeypatch.setattr(module, "_available_memory", lambda: 48 * 1024**3)
    (tmp_path / "cpu").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "cpu/cpu.cfs_quota_us").write_text("400000")
    (tmp_path / "cpu/cpu.cfs_period_us").write_text("100000")
    (tmp_path / "memory/memory.limit_in_bytes").write_text(str(2 * 1024**3))
    (tmp_path / "memory/memory.usage_in_bytes").write_text(str(2 * 1024**3))
    resources = detect_import_resources(cgroup_path=tmp_path, cgroup_root=tmp_path)
    assert resources.cpu_count == 4
    assert resources.available_memory_bytes == 0
    assert resources.inspection_workers() == 1


@pytest.mark.parametrize("bad_quota", ["garbage", "-1 100000", "1000 0", "max 100000"])
def test_unlimited_or_malformed_cpu_quota_does_not_break_import(tmp_path, monkeypatch, bad_quota):
    import pullbox.core.import_resources as module

    monkeypatch.setattr(module, "_visible_cpus", lambda: 8)
    monkeypatch.setattr(module, "_available_memory", lambda: 8 * 1024**3)
    (tmp_path / "cpu.max").write_text(bad_quota)
    resources = detect_import_resources(cgroup_path=tmp_path, cgroup_root=tmp_path)
    assert resources.cpu_count == 8
    assert resources.inspection_workers() == 4
