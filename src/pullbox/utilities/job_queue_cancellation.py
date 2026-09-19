"""Observe committed cancellation while retaining ownership of active workers."""

from __future__ import annotations

import asyncio
from typing import Any

from pullbox.services.utility_operation_progress import project_utility_operation_progress
from pullbox.utilities.job_queue_state import transition_job_state
from pullbox.utilities.models import JobState, UtilityJob


async def drain_task(task: asyncio.Task[Any]) -> Any:
    """Do not abandon a worker/result journal when the dispatcher is cancelled."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


async def wait_for_dispatch_batch(
    task: asyncio.Task[None],
    *,
    session_factory: Any,
    job_id: str,
    worker_pool: Any,
) -> None:
    """Signal cooperative workers, then wait for all source mutations and journals."""
    request_cancel = getattr(worker_pool, "request_cancel", lambda: None)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if done:
                break
            async with session_factory() as session:
                job = await session.get(UtilityJob, job_id)
                if job is None or job.state in {JobState.CANCELLING, JobState.CANCELLED}:
                    request_cancel()
        await task
    except asyncio.CancelledError:
        request_cancel()

        # Persist the interrupted dispatch intent before draining results. A crash
        # during the drain will be recovered as cancelled on the next startup.
        async def record_and_drain() -> None:
            try:
                async with session_factory() as session:
                    job = await session.get(UtilityJob, job_id)
                    if job is not None and job.state in {JobState.RUNNING, JobState.PAUSING}:
                        transition_job_state(job, JobState.CANCELLING)
                        await project_utility_operation_progress(session, job)
                        await session.commit()
            finally:
                await drain_task(task)

        await drain_task(asyncio.create_task(record_and_drain()))
        raise
    except Exception:
        request_cancel()
        await drain_task(task)
        raise
