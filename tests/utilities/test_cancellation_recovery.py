"""Regression coverage for abandoned utility cancellations and safe completion."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.models.base import Base
from pullbox.models.operation_progress import OperationProgress
from pullbox.services.utility_operation_progress import project_utility_operation_progress
from pullbox.utilities.base_executor import ExecutionMode, FinalizeResult
from pullbox.utilities.cancellation import check_cancelled, worker_cancellation
from pullbox.utilities.executors.file_converter import FileConverterExecutor
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.job_queue_batch_state import lease_dispatch_batch
from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)
from pullbox.utilities.worker_pool import WorkerPool
from tests.utilities.conftest import StubExecutor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[Any]:
    # Separate connections/transactions, like production. Cancelling a query can
    # discard its connection, which would erase a shared in-memory test database.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def make_job(session: Any, state: JobState = JobState.CANCELLING) -> UtilityJob:
    manager = JobQueueManager(None)
    job = await manager.create_job(session, JobType.MASS_CONVERT_PIPELINE, "Convert", {})
    job.state = state
    await project_utility_operation_progress(session, job)
    await session.commit()
    return job


async def test_restart_finishes_cancel_preserving_completed_work(session_factory: Any) -> None:
    async with session_factory() as session:
        job = await make_job(session)
        job.total_items = 48
        job.completed_items = 36
        for index in range(48):
            session.add(
                UtilityJobItem(
                    id=f"item-{index}",
                    job_id=job.id,
                    item_index=index,
                    operation="pipeline",
                    state=ItemState.COMPLETED if index < 36 else ItemState.PENDING,
                    before_state=json.dumps({"original": index}),
                    after_state=json.dumps({"trash": index}) if index < 36 else None,
                )
            )
        await session.commit()
        before = list((await session.execute(select(UtilityJobItem))).scalars())
        snapshots = [(item.state, item.before_state, item.after_state) for item in before]
        manager = JobQueueManager(session_factory)
        count = await manager.recover_interrupted_jobs(session)
        await session.commit()
        assert count == 1
        await session.refresh(job)
        assert job.state == JobState.CANCELLED
        assert job.completed_at and job.queue_position is None
        assert (job.completed_items, job.failed_items, job.skipped_items) == (36, 0, 0)
        for item, snapshot in zip(before, snapshots, strict=True):
            await session.refresh(item)
            assert (item.state, item.before_state, item.after_state) == snapshot
        progress = (
            await session.execute(
                select(OperationProgress).where(OperationProgress.operation_key == job.id)
            )
        ).scalar_one()
        assert progress.state == "cancelled"
        assert progress.completed_at is not None
        assert progress.item_key is None
        assert await manager.recover_interrupted_jobs(session) == 0


async def test_cancelled_job_cannot_lease_another_batch(session_factory: Any) -> None:
    async with session_factory() as session:
        job = await make_job(session)
        item = UtilityJobItem(
            id="pending",
            job_id=job.id,
            item_index=0,
            state=ItemState.PENDING,
            operation="pipeline",
        )
        session.add(item)
        await session.commit()
        leased = await lease_dispatch_batch(
            session,
            pending_items=[item],
            batch_start=0,
            batch_size=1,
            started_at="now",
        )
        assert leased == []
        await session.refresh(item)
        assert item.state == ItemState.PENDING


async def test_restart_records_uncertain_in_progress_file_before_clearing_lease(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        job = await make_job(session)
        item = UtilityJobItem(
            id="interrupted",
            job_id=job.id,
            item_index=0,
            state=ItemState.IN_PROGRESS,
            operation="pipeline",
            file_path="/comics/example.cbr",
            before_state='{"source":"retained"}',
            after_state='{"trash":"retained"}',
        )
        session.add(item)
        await session.commit()
        await JobQueueManager(session_factory).recover_interrupted_jobs(session)
        await session.commit()
        logs = list(
            (
                await session.execute(select(UtilityJobLog).where(UtilityJobLog.item_id == item.id))
            ).scalars()
        )
        assert len(logs) == 1 and logs[0].level == "WARNING"
        assert logs[0].file_path == "/comics/example.cbr"
        assert "interrupted" in logs[0].message.lower()
        await session.refresh(item)
        assert item.state == ItemState.PENDING
        assert item.before_state == '{"source":"retained"}'
        assert item.after_state == '{"trash":"retained"}'


@pytest.mark.parametrize("state", [JobState.CANCELLING, JobState.CANCELLED, JobState.PAUSING])
async def test_cancel_can_be_repeated_and_overrides_pause(
    session_factory: Any, state: JobState
) -> None:
    async with session_factory() as session:
        job = await make_job(session, state)
        manager = JobQueueManager(session_factory)
        error = None
        try:
            await manager.cancel_job(session, job.id)
            await manager.cancel_job(session, job.id)
        except ValueError as exc:
            error = exc
        assert error is None, f"Cancel should be idempotent: {error}"
        assert job.state == (
            JobState.CANCELLED if state == JobState.CANCELLED else JobState.CANCELLING
        )


async def test_repeated_cancel_does_not_duplicate_rollback(session_factory: Any) -> None:
    async with session_factory() as session:
        job = await make_job(session, JobState.RUNNING)
        manager = JobQueueManager(session_factory)
        await manager.cancel_job(session, job.id, rollback=True)
        error = None
        try:
            await manager.cancel_job(session, job.id, rollback=True)
        except ValueError as exc:
            error = exc
        assert error is None, f"Repeated cancel must not create or reject another rollback: {error}"
        children = list(
            (
                await session.execute(select(UtilityJob).where(UtilityJob.parent_job_id == job.id))
            ).scalars()
        )
        assert len(children) == 1


class BrokenFinalizer(StubExecutor):
    execution_mode = ExecutionMode.THREAD

    async def finalize_job(self, session: Any, job: Any, *args: Any) -> FinalizeResult:
        raise RuntimeError("finalizer unavailable")


async def test_finalizer_failure_does_not_leave_a_running_job(session_factory: Any) -> None:
    manager = JobQueueManager(session_factory)
    manager.register_executor(JobType.FILE_CONVERT, BrokenFinalizer)
    async with session_factory() as session:
        job = await manager.create_job(session, JobType.FILE_CONVERT, "Test", {"count": 1})
        await session.commit()
        job_id = job.id
    error = None
    try:
        await manager.dispatch_next()
    except RuntimeError as exc:
        error = exc
    assert error is None, f"Finalization failure must be durably recorded: {error}"
    async with session_factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job is not None and job.state == JobState.FAILED
        assert job.completed_items == 1
        assert "finalizer unavailable" in job.error_message
        progress = (
            await session.execute(
                select(OperationProgress).where(OperationProgress.operation_key == job_id)
            )
        ).scalar_one()
        assert progress.state == "failed"


async def test_cancelling_job_survives_finalizer_error(
    session_factory: Any,
    monkeypatch: Any,
) -> None:
    manager = JobQueueManager(session_factory)
    manager.register_executor(JobType.FILE_CONVERT, BrokenFinalizer)
    project = manager._project_dispatch_progress

    async def cancel_after_batch(job_id: str, item_id: str | None = None) -> None:
        await project(job_id, item_id)
        if item_id is None:
            async with session_factory() as session:
                await manager.cancel_job(session, job_id)
                await session.commit()

    monkeypatch.setattr(manager, "_project_dispatch_progress", cancel_after_batch)
    async with session_factory() as session:
        job = await manager.create_job(session, JobType.FILE_CONVERT, "Test", {"count": 1})
        await session.commit()
        job_id = job.id
    await manager.dispatch_next()
    async with session_factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.CANCELLED
        assert job.completed_items == 1 and job.failed_items == 0
        assert "finalizer unavailable" in job.error_message
        progress = (
            await session.execute(
                select(OperationProgress).where(OperationProgress.operation_key == job_id)
            )
        ).scalar_one()
        assert progress.state == "cancelled"


async def test_long_archive_child_is_stopped_and_partial_output_removed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from pullbox.utilities.executors import archive_subprocess

    source = tmp_path / "comic.cbz"
    source.write_bytes(b"original comic")
    marker = tmp_path / "cancel"
    partial = tmp_path / "comic._mass_convert_.cbz"
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def slow_archive(*args: Any, **kwargs: Any) -> Any:
        process = await spawn(
            sys.executable,
            "-c",
            "import pathlib,time,sys; pathlib.Path(sys.argv[1]).write_bytes(b'partial'); "
            "time.sleep(30)",
            str(partial),
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", slow_archive)
    with worker_cancellation(str(marker)):
        task = asyncio.create_task(
            asyncio.to_thread(
                MassConvertPipelineExecutor().process_item,
                {"id": "item", "file_path": str(source)},
                {"steps": [1], "trash_folder": str(tmp_path / "trash")},
            )
        )
        try:
            async with asyncio.timeout(5):
                while not partial.exists():
                    await asyncio.sleep(0.01)
            marker.touch()
            result = await asyncio.wait_for(asyncio.shield(task), 5)
        finally:
            marker.touch()
            await task
    assert str(result.result) == "cancelled"
    assert len(processes) == 1 and processes[0].returncode is not None
    assert source.read_bytes() == b"original comic"
    assert not partial.exists()
    assert not (tmp_path / "trash").exists()


@pytest.mark.parametrize("state", [JobState.CANCELLING, JobState.PAUSING])
async def test_draining_worker_keeps_serial_queue_slot(
    session_factory: Any, state: JobState
) -> None:
    manager = JobQueueManager(session_factory)
    manager.register_executor(JobType.FILE_CONVERT, BrokenFinalizer)
    async with session_factory() as session:
        await make_job(session, state)
        queued = await manager.create_job(session, JobType.FILE_CONVERT, "Next", {"count": 0})
        await session.commit()
        job_id = queued.id
    with suppress(RuntimeError):
        await manager.dispatch_next()
    async with session_factory() as session:
        queued = await session.get(UtilityJob, job_id)
        assert queued is not None and queued.state == JobState.QUEUED


class CooperativeExecutor(StubExecutor):
    execution_mode = ExecutionMode.THREAD

    def process_item(self, item_data: Any, config: Any) -> Any:
        Path(config["started"]).touch()
        for _ in range(400):
            check_cancelled()
            time.sleep(0.01)
        return super().process_item(item_data, config)


def test_cancel_during_source_replacement_finishes_and_retains_rollback_journal(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from pullbox.utilities.executors import file_converter, mass_convert_pipeline

    source = tmp_path / "comic.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"page")
    original = source.read_bytes()
    marker = tmp_path / "cancel"
    trash = tmp_path / "trash"
    move = mass_convert_pipeline.move_file_to_utility_trash

    def cancel_after_move(*args: Any, **kwargs: Any) -> Path:
        path = move(*args, **kwargs)
        marker.touch()
        return path

    monkeypatch.setattr(mass_convert_pipeline, "convert_utility_file", file_converter._convert_sync)
    monkeypatch.setattr(
        mass_convert_pipeline, "_resolve_effective_trash_directory", lambda _: trash
    )
    monkeypatch.setattr(mass_convert_pipeline, "move_file_to_utility_trash", cancel_after_move)
    with worker_cancellation(str(marker)):
        result = MassConvertPipelineExecutor().process_item(
            {"id": "item", "file_path": str(source)},
            {"steps": [1]},
        )
    assert str(result.result) == "completed"
    assert marker.exists()
    assert Path(result.after_state["original_path"]).read_bytes() == original
    assert Path(result.after_state["path"]) == source and source.is_file()


@pytest.mark.parametrize("mode", [ExecutionMode.THREAD, ExecutionMode.PROCESS])
async def test_worker_cancel_stops_cooperative_work(tmp_path: Path, mode: ExecutionMode) -> None:
    pool = WorkerPool(execution_mode=mode, max_workers=1)
    started = tmp_path / "started"
    task = asyncio.create_task(
        pool.process_batch(
            [{"id": "first"}, {"id": "second"}], CooperativeExecutor(), {"started": str(started)}
        )
    )
    try:
        async with asyncio.timeout(10):
            while not started.exists():
                await asyncio.sleep(0.01)
        pool.request_cancel()
        results = await asyncio.wait_for(asyncio.shield(task), 2)
        assert [str(result.result) for result in results] == ["cancelled", "cancelled"]
    finally:
        await task
        await asyncio.to_thread(pool.shutdown)


@pytest.mark.parametrize("executor_type", [MassConvertPipelineExecutor, FileConverterExecutor])
def test_conversion_cancel_before_publish_keeps_original(
    tmp_path: Path,
    monkeypatch: Any,
    executor_type: Any,
) -> None:
    source = tmp_path / "comic.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"page")
    original = source.read_bytes()
    marker = tmp_path / "cancel"
    from pullbox.utilities.executors import file_converter, mass_convert_pipeline

    convert = file_converter._convert_sync

    def cancel_after_convert(*args: Any, **kwargs: Any) -> Any:
        result = convert(*args, **kwargs)
        marker.touch()
        return result

    monkeypatch.setattr(mass_convert_pipeline, "convert_utility_file", cancel_after_convert)
    from pullbox.utilities.executors import utility_archive_work

    monkeypatch.setattr(utility_archive_work, "convert_utility_file", cancel_after_convert)
    with worker_cancellation(str(marker)):
        result = executor_type().process_item(
            {"id": "item", "file_path": str(source)},
            {"steps": [1], "trash_folder": str(tmp_path / "trash")},
        )
    assert str(result.result) == "cancelled"
    assert source.read_bytes() == original
    assert not (tmp_path / "comic._mass_convert_.cbz").exists()
    assert not (tmp_path / "comic._repack_.cbz").exists()
    assert not (tmp_path / "trash").exists()


async def test_cancel_interrupts_active_dispatch_without_counting_failures(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    manager = JobQueueManager(session_factory)
    manager.register_executor(JobType.FILE_CONVERT, CooperativeExecutor)
    started = tmp_path / "started"
    async with session_factory() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_CONVERT,
            "Test",
            {
                "count": 2,
                "started": str(started),
            },
        )
        await session.commit()
        job_id = job.id
    task = asyncio.create_task(manager.dispatch_next())
    try:
        async with asyncio.timeout(10):
            while not started.exists():
                await asyncio.sleep(0.01)
        async with session_factory() as session:
            await manager.cancel_job(session, job_id)
            await session.commit()
        await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        await task
    async with session_factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job is not None and job.state == JobState.CANCELLED
        assert (job.completed_items, job.failed_items, job.skipped_items) == (0, 0, 0)
        items = list(
            (
                await session.execute(select(UtilityJobItem).where(UtilityJobItem.job_id == job_id))
            ).scalars()
        )
        assert len(items) == 2 and all(item.state == ItemState.PENDING for item in items)


async def test_dispatch_task_cancellation_drains_workers_and_finalizes(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    manager = JobQueueManager(session_factory)
    manager.register_executor(JobType.FILE_CONVERT, CooperativeExecutor)
    started = tmp_path / "started"
    async with session_factory() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_CONVERT,
            "Test",
            {
                "count": 1,
                "started": str(started),
            },
        )
        await session.commit()
        job_id = job.id
    task = asyncio.create_task(manager.dispatch_next())
    async with asyncio.timeout(10):
        while not started.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with session_factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job is not None and job.state == JobState.CANCELLED
        assert job.completed_items == 0
        items = list(
            (
                await session.execute(select(UtilityJobItem).where(UtilityJobItem.job_id == job_id))
            ).scalars()
        )
        assert all(item.state == ItemState.PENDING for item in items)
        progress = (
            await session.execute(
                select(OperationProgress).where(OperationProgress.operation_key == job_id)
            )
        ).scalar_one()
        assert progress.state == "cancelled"
