"""Conservative container-aware budgets for read-only import inspection."""

from __future__ import annotations

import asyncio
import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable

_MIB = 1024**2


@dataclass(frozen=True)
class ImportResources:
    cpu_count: int
    available_memory_bytes: int

    def inspection_workers(self, *, requested: int = 0) -> int:
        """Reserve CPU and memory for requests and other running services."""
        cpu_budget = max(1, self.cpu_count - math.ceil(self.cpu_count / 4))
        memory_budget = max(1, (self.available_memory_bytes - 512 * _MIB) // (512 * _MIB))
        ceiling = max(1, min(cpu_budget, memory_budget, 16))
        # Local SSD measurements show no gain from saturating the CPU with ZIP parsing.
        return min(ceiling, requested) if requested > 0 else min(ceiling, 4)


def _visible_cpus() -> int:
    try:
        affinity_reader = getattr(psutil.Process(), "cpu_affinity", None)
        affinity = affinity_reader() if affinity_reader is not None else []
        if affinity:
            return len(affinity)
    except (AttributeError, OSError, psutil.Error):
        pass
    return os.cpu_count() or 1


def _available_memory() -> int:
    try:
        return int(psutil.virtual_memory().available)
    except (OSError, psutil.Error):
        return 0


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def _positive_integer(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def detect_import_resources(
    *, cgroup_path: Path | None = None, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> ImportResources:
    """Use visible resources, honoring cgroup v2 ancestors and v1 limits."""
    cpus = max(1, _visible_cpus())
    memory = max(0, _available_memory())
    if cgroup_path is None:
        cgroup_path = cgroup_root
        for line in _read(Path("/proc/self/cgroup")).splitlines():
            if line.startswith("0::"):
                candidate = cgroup_root / line[3:].lstrip("/")
                if ".." not in candidate.parts and candidate.is_dir():
                    cgroup_path = candidate
                break
    groups = [cgroup_path]
    if cgroup_path.is_relative_to(cgroup_root):
        groups.extend(
            parent for parent in cgroup_path.parents if parent.is_relative_to(cgroup_root)
        )
    for group in groups:
        quota = _read(group / "cpu.max").split()
        if len(quota) == 2:
            limit, period = (_positive_integer(value) for value in quota)
            if limit is not None and period:
                cpus = min(cpus, max(1, limit // period))
        maximum = _positive_integer(_read(group / "memory.max"))
        used = _positive_integer(_read(group / "memory.current"))
        if maximum is not None:
            memory = min(memory, max(0, maximum - used)) if used is not None else 0
    # Common v1 container mounts. Affinity also accounts for cpuset restrictions.
    quota_v1 = _positive_integer(_read(cgroup_root / "cpu/cpu.cfs_quota_us"))
    period_v1 = _positive_integer(_read(cgroup_root / "cpu/cpu.cfs_period_us"))
    if quota_v1 is not None and period_v1:
        cpus = min(cpus, max(1, quota_v1 // period_v1))
    maximum_v1 = _positive_integer(_read(cgroup_root / "memory/memory.limit_in_bytes"))
    used_v1 = _positive_integer(_read(cgroup_root / "memory/memory.usage_in_bytes"))
    if maximum_v1 is not None:
        memory = min(memory, max(0, maximum_v1 - used_v1)) if used_v1 is not None else 0
    return ImportResources(cpus, memory)


@asynccontextmanager
async def bounded_thread_map[Input, Output](
    function: Callable[[Input], Output], values: Iterable[Input], *, workers: int
) -> AsyncIterator[AsyncIterator[Output]]:
    """Run bounded filesystem work without passing a database session to threads."""

    async def run(value: Input) -> Output:
        return await asyncio.to_thread(function, value)

    async with bounded_async_map(run, values, workers=workers) as results:
        yield results


@asynccontextmanager
async def bounded_async_map[Input, Output](
    function: Callable[[Input], Awaitable[Output]], values: Iterable[Input], *, workers: int
) -> AsyncIterator[AsyncIterator[Output]]:
    """Yield completed work with bounded submission and drain active work on exit.

    Canceling an asyncio wrapper cannot stop a running filesystem operation.
    Keep ownership until it finishes, including when the consumer raises or exits.
    """
    source = iter(values)
    tasks: set[asyncio.Task[Output]] = set()

    async def run(item: Input) -> Output:
        return await function(item)

    async def results() -> AsyncGenerator[Output, None]:
        exhausted = False
        while tasks or not exhausted:
            while not exhausted and len(tasks) < max(1, workers):
                try:
                    value = next(source)
                except StopIteration:
                    exhausted = True
                else:
                    tasks.add(asyncio.create_task(run(value)))
            if not tasks:
                break
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                tasks.remove(task)
                yield result

    iterator = results()
    try:
        yield iterator
    finally:
        await iterator.aclose()
        if tasks:
            drain = asyncio.gather(*tasks, return_exceptions=True)
            interrupted = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError
