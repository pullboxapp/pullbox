"""Import controls must not lose a wakeup while the previous worker exits."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.tasks import import_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_status", "phase", "expected_runs"),
    [
        (ImportJobStatus.IMPORTING, "queued", 2),
        (ImportJobStatus.FILE_MATCHING, "file_matching", 2),
        (ImportJobStatus.ROLLING_BACK, "rollback", 2),
        (ImportJobStatus.PAUSED, "importing", 1),
        (ImportJobStatus.STALLED, "importing", 1),
        (ImportJobStatus.REVIEW, "review", 1),
        (ImportJobStatus.COMPLETED, "done", 1),
        (ImportJobStatus.FAILED, "failed", 1),
        (ImportJobStatus.CANCELLED, "cancelled", 1),
        (ImportJobStatus.ROLLED_BACK, "done", 1),
        (ImportJobStatus.IMPORTING, "story_arc_placements", 1),
        (None, "deleted", 1),
    ],
)
async def test_control_wakeup_survives_worker_handoff_without_replaying_finished_work(
    async_engine: AsyncEngine,
    final_status: ImportJobStatus | None,
    phase: str,
    expected_runs: int,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        job = ImportJob(
            source_path="/imports/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
            import_started_at=datetime.now(UTC),
        )
        session.add(job)
        await session.commit()
        job_id = int(job.id)

    runner = import_task.ImportRunner(factory)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []
    running = 0
    maximum_running = 0

    async def run_job(requested_id: int) -> None:
        nonlocal running, maximum_running
        running += 1
        maximum_running = max(maximum_running, running)
        calls.append(requested_id)
        try:
            if len(calls) == 1:
                started.set()
                await release.wait()
            async with factory() as session:
                current = await session.get(ImportJob, requested_id)
                assert current is not None
                if final_status is None:
                    await session.delete(current)
                else:
                    current.status = final_status if len(calls) == 1 else ImportJobStatus.COMPLETED
                    current.progress_snapshot = {"phase": phase}
                await session.commit()
        finally:
            running -= 1

    runner._run_job = run_job  # type: ignore[method-assign]
    await runner.request_execute(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    worker = runner._worker_task
    assert worker is not None
    # The API has accepted a control action while the old worker still owns
    # the runner. Repeated notifications must coalesce, not run concurrently.
    await runner.request_resume(job_id)
    await runner.request_execute(job_id)
    await runner.request_resume(job_id)
    release.set()
    await asyncio.wait_for(worker, timeout=2)
    await asyncio.sleep(0)
    if import_task._background_tasks:
        await asyncio.wait_for(asyncio.gather(*import_task._background_tasks), timeout=2)
    if runner._worker_task is not None:
        await asyncio.wait_for(runner._worker_task, timeout=2)

    assert calls == [job_id] * expected_runs
    assert maximum_running == 1
