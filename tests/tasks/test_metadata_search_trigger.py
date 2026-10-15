"""Sweep coordination contracts; real source/cadence coverage lives in metadata_identity."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.metadata_identity import MetadataSource
from pullbox.models import MetadataSeriesRetry, Series
from pullbox.schemas.metadata_sources import SourceOutcome, SourceStatus
from pullbox.services.metadata_scheduled_refresh import ScheduledSeriesRefresh
from pullbox.services.metadata_series_refresh import SeriesRefreshError
from pullbox.tasks import metadata_task
from pullbox.tasks.metadata_sweep_state import MetadataSweep, load_sweep, save_sweep
from tests.sqlite_schema import create_test_schema

_MOD = "pullbox.tasks.metadata_task"


@pytest.fixture
async def restore_db_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restore.db'}")
    async with engine.begin() as conn:
        await create_test_schema(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _create_series(factory, *, comicvine_id):
    async with factory.begin() as session:
        series = Series(
            comicvine_id=comicvine_id, title="Example", sort_title="Example", monitored=True
        )
        session.add(series)
        await session.flush()
        return series.id


def _make_scheduler():
    return MagicMock()


def _make_metadata_svc(_unused):
    return AsyncMock()


@contextlib.contextmanager
def _sync_patches(factory, service, scheduler, *, search=False):
    async def refresh(session, series_id, *, registry):
        await service.refresh_series(session, series_id)
        return ScheduledSeriesRefresh(int(search), search, None, Path("/unused-test-covers"))

    async def daily(session, series_id, *, refresh_days, registry):
        await service.fetch_series(session, series_id)
        return ScheduledSeriesRefresh(int(search), search, None, Path("/unused-test-covers"))

    with (
        patch(f"{_MOD}.get_session_factory", return_value=factory),
        patch(
            f"{_MOD}.scheduled_series_eligibility",
            AsyncMock(return_value=Series.comicvine_id.isnot(None)),
        ),
        patch(f"{_MOD}.refresh_scheduled_series", side_effect=refresh),
        patch(f"{_MOD}.sync_scheduled_issue_catalog", side_effect=daily),
        patch(f"{_MOD}.get_scheduler", return_value=scheduler),
        patch("pullbox.tasks.metadata_sweep_state.get_scheduler", return_value=scheduler),
    ):
        yield


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
@pytest.mark.parametrize("restart", [False, True])
async def test_restore_waits_for_remaining_metadata_batches(
    restore_db_factory, tmp_path, monkeypatch, task_id, restart
):
    from pullbox.services import restore_recovery_service as service
    from pullbox.tasks import metadata_task
    from pullbox.tasks.metadata_sweep_state import load_sweep

    db_factory = restore_db_factory
    for identifier in (91001, 91002, 91003):
        await _create_series(db_factory, comicvine_id=identifier)
    monkeypatch.setattr(metadata_task, "_METADATA_BATCH_SIZE", 2)
    monkeypatch.setattr(
        service, "_run_cover_backfill_step", AsyncMock(return_value="Covers checked")
    )
    other_step = (
        "_run_metadata_refresh_step" if task_id == "sync_new_issues" else "_run_issue_sync_step"
    )
    monkeypatch.setattr(service, other_step, AsyncMock(return_value="Other step checked"))
    monkeypatch.setattr("pullbox.database.get_session_factory", lambda: db_factory)
    # The observer must await the scheduler's next batch, not run a second sweep itself.
    monkeypatch.setattr(service, "_SWEEP_POLL_SECONDS", 0.01, raising=False)
    svc = _make_metadata_svc([])
    service.mark_restore_recovery_pending("restore.zip", data_dir=tmp_path)
    restore_task = None
    try:
        with _sync_patches(db_factory, svc, _make_scheduler()):
            restore_task = asyncio.create_task(
                service.run_restore_recovery_if_pending(data_dir=tmp_path)
            )
            async with asyncio.timeout(3):
                while True:
                    async with db_factory() as session:
                        state = await load_sweep(session, task_id)
                    if state.active and state.cursor == 2:
                        break
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
            assert not restore_task.done(), "Restore must not finish with a pending metadata batch"
            assert service.has_pending_restore_recovery(data_dir=tmp_path)
            assert service.get_restore_recovery_status(data_dir=tmp_path)["status"] == "running"
            if restart:
                restore_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await restore_task
                assert service.has_pending_restore_recovery(data_dir=tmp_path)
                restore_task = asyncio.create_task(
                    service.run_restore_recovery_if_pending(data_dir=tmp_path)
                )
            else:
                await getattr(metadata_task, task_id)()
            result = await asyncio.wait_for(restore_task, 3)
        assert result["status"] == "completed"
        assert not service.has_pending_restore_recovery(data_dir=tmp_path)
        calls = svc.fetch_series if task_id == "sync_new_issues" else svc.refresh_series
        assert calls.await_count == 3
    finally:
        if restore_task is not None and not restore_task.done():
            restore_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restore_task


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_no_eligible_source_stops_an_active_continuation(restore_db_factory, task_id):
    async with restore_db_factory.begin() as session:
        await save_sweep(
            session, task_id, MetadataSweep(cursor=1, upper_bound=3, retry_at=100, active=True)
        )
    scheduler = _make_scheduler()
    service = _make_metadata_svc([])
    with _sync_patches(restore_db_factory, service, scheduler):
        result = await getattr(metadata_task, task_id)()
    assert result.status == "completed"
    async with restore_db_factory() as session:
        state = await load_sweep(session, task_id)
        assert not state.active and state.retry_at == 0
    scheduler.clear_task_continuation.assert_called_once_with(task_id)
    service.fetch_series.assert_not_awaited()
    service.refresh_series.assert_not_awaited()


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_interrupted_sweep_restarts_at_first_uncommitted_series(restore_db_factory, task_id):
    factory = restore_db_factory
    ids = [await _create_series(factory, comicvine_id=i) for i in (91001, 91002, 91003)]
    service = _make_metadata_svc([])
    operation = service.fetch_series if task_id == "sync_new_issues" else service.refresh_series
    blocked = asyncio.Event()

    async def fetch(session, series_id):
        if series_id == ids[1]:
            blocked.set()
            await asyncio.Event().wait()

    operation.side_effect = fetch
    with _sync_patches(factory, service, _make_scheduler()):
        task = asyncio.create_task(getattr(metadata_task, task_id)())
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            operation.side_effect = None
            await getattr(metadata_task, task_id)()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert [call.args[1] for call in operation.await_args_list] == [ids[0], ids[1], ids[1], ids[2]]


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_bounded_sweep_keeps_initial_upper_bound_and_commits_searches(
    restore_db_factory, monkeypatch, task_id
):
    factory = restore_db_factory
    ids = [await _create_series(factory, comicvine_id=i) for i in (91001, 91002, 91003)]
    service = _make_metadata_svc([])
    operation = service.fetch_series if task_id == "sync_new_issues" else service.refresh_series
    scheduler = _make_scheduler()
    monkeypatch.setattr(metadata_task, "_METADATA_BATCH_SIZE", 2)
    with _sync_patches(factory, service, scheduler, search=True):
        assert (await getattr(metadata_task, task_id)()).status == "waiting"
        assert operation.await_count == 2
        async with factory() as session:
            state = await load_sweep(session, task_id)
            assert state.cursor == ids[1] and state.upper_bound == ids[-1]
        later = await _create_series(factory, comicvine_id=91004)
        assert (await getattr(metadata_task, task_id)()).status == "completed"
        assert [call.args[1] for call in operation.await_args_list] == ids
        assert later not in ids
    assert [call.kwargs["args"][0] for call in scheduler._scheduler.add_job.call_args_list] == ids


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_provider_retry_retains_cursor_and_survives_restart(restore_db_factory, task_id):
    factory = restore_db_factory
    series_id = await _create_series(factory, comicvine_id=91001)
    service = _make_metadata_svc([])
    operation = service.fetch_series if task_id == "sync_new_issues" else service.refresh_series
    operation.side_effect = SeriesRefreshError(
        "Source unavailable",
        outcomes=(
            SourceOutcome(
                source=MetadataSource.METRON_API,
                status=SourceStatus.RATE_LIMITED,
                retry_after_seconds=720,
            ),
        ),
    )
    scheduler = _make_scheduler()
    with _sync_patches(factory, service, scheduler):
        assert (await getattr(metadata_task, task_id)()).status == "waiting"
        assert (await getattr(metadata_task, task_id)()).status == "waiting"
    operation.assert_awaited_once()
    async with factory() as session:
        state = await load_sweep(session, task_id)
        assert state.cursor == series_id and state.active and state.retry_at > 0
        retry = await session.scalar(select(MetadataSeriesRetry))
        assert retry.source == MetadataSource.METRON_API.value
        assert retry.retry_at.timestamp() == state.retry_at
    scheduler._scheduler.add_job.assert_not_called()


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_structural_failure_rolls_back_one_series_and_continues(restore_db_factory, task_id):
    factory = restore_db_factory
    ids = [await _create_series(factory, comicvine_id=i) for i in (91001, 91002)]
    service = _make_metadata_svc([])
    operation = service.fetch_series if task_id == "sync_new_issues" else service.refresh_series

    async def fetch(session, series_id):
        series = await session.get(Series, series_id)
        series.description = "New metadata"
        if series_id == ids[0]:
            await session.flush()
            raise SeriesRefreshError("Identity needs review")

    operation.side_effect = fetch
    scheduler = _make_scheduler()
    with _sync_patches(factory, service, scheduler, search=True):
        assert (await getattr(metadata_task, task_id)()).status == "completed"
    async with factory() as session:
        assert (await session.get(Series, ids[0])).description is None
        assert (await session.get(Series, ids[1])).description == "New metadata"
    scheduler._scheduler.add_job.assert_called_once()
    assert scheduler._scheduler.add_job.call_args.kwargs["args"] == [ids[1]]
