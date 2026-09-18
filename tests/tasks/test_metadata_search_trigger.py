"""Tests for the new-issue search trigger in sync_new_issues.

Verifies:
- When sync finds new issues and series is monitored, search is scheduled
- No search triggered for unmonitored series
- No search triggered if sync finds issues but none set to WANTED
- New issues have monitoring criteria applied (SKIPPED → WANTED)

Run:
    pytest tests/tasks/test_metadata_search_trigger.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-metadata-search")

_MOD = "pullbox.tasks.metadata_task"


@pytest.mark.parametrize("task_id", ["sync_new_issues", "refresh_metadata"])
async def test_missing_key_stops_active_metadata_continuation(db_factory, task_id):
    from pullbox.tasks import metadata_task
    from pullbox.tasks.metadata_sweep_state import MetadataSweep, load_sweep, save_sweep

    async with db_factory() as session:
        await save_sweep(
            session, task_id, MetadataSweep(cursor=1, upper_bound=3, retry_at=100, active=True)
        )
        await session.commit()
    scheduler = _make_scheduler()
    svc = _make_metadata_svc([])
    with (
        _sync_patches(db_factory, svc, scheduler),
        patch(f"{_MOD}.get_comicvine_api_key", new_callable=AsyncMock, return_value=None),
    ):
        await getattr(metadata_task, task_id)()
    async with db_factory() as session:
        state = await load_sweep(session, task_id)
    assert state.active is False, "A missing key must not leave a minute-by-minute continuation"
    assert state.retry_at == 0
    scheduler.clear_task_continuation.assert_called_once_with(task_id)
    scheduler.schedule_task_continuation.assert_not_called()
    svc.fetch_series.assert_not_awaited()


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


async def test_interrupted_sync_restarts_at_first_uncommitted_series(db_factory):
    from pullbox.tasks import metadata_task

    ids = [await _create_series(db_factory, comicvine_id=i) for i in (91001, 91002, 91003)]
    blocked = asyncio.Event()
    svc = _make_metadata_svc([])

    async def fetch(session, series_id):
        if series_id == ids[1]:
            blocked.set()
            await asyncio.Event().wait()
        return []

    svc.fetch_issues_for_series.side_effect = fetch
    with _sync_patches(db_factory, svc, _make_scheduler()):
        task = asyncio.create_task(metadata_task.sync_new_issues())
        await asyncio.wait_for(blocked.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        svc.fetch_issues_for_series.side_effect = None
        svc.fetch_issues_for_series.return_value = []
        await metadata_task.sync_new_issues()
    assert [call.args[1] for call in svc.fetch_series.await_args_list] == [
        91001,
        91002,
        91002,
        91003,
    ]


async def test_sync_resumes_bounded_batches_from_durable_cursor(db_factory, monkeypatch):
    from pullbox.tasks import metadata_task

    for identifier in (91001, 91002, 91003):
        await _create_series(db_factory, comicvine_id=identifier)
    svc = _make_metadata_svc([])
    scheduler = _make_scheduler()
    monkeypatch.setattr(metadata_task, "_METADATA_BATCH_SIZE", 2, raising=False)
    with _sync_patches(db_factory, svc, scheduler):
        result = await metadata_task.sync_new_issues()
        assert svc.fetch_series.await_count == 2, "A run must yield after a bounded batch"
        assert result.status == "waiting"
        await metadata_task.sync_new_issues()
    assert [call.args[1] for call in svc.fetch_series.await_args_list] == [91001, 91002, 91003]
    scheduler.schedule_task_continuation.assert_called()


async def test_sync_pauses_whole_sweep_after_provider_throttle(db_factory):
    from pullbox.core.exceptions import ProviderError
    from pullbox.tasks import metadata_task

    for identifier in (91001, 91002, 91003):
        await _create_series(db_factory, comicvine_id=identifier)
    svc = _make_metadata_svc([])
    svc.fetch_series.side_effect = ProviderError(
        "comicvine", "HTTP 420", details={"status_code": 420, "retryable": True}
    )
    with _sync_patches(db_factory, svc, _make_scheduler()):
        result = await metadata_task.sync_new_issues()
        assert svc.fetch_series.await_count == 1, (
            "One throttle must pause the provider, not fail every series"
        )
        assert result.status == "waiting"
        await metadata_task.sync_new_issues()
        assert svc.fetch_series.await_count == 1, "Persisted cooldown must survive a fresh batch"


async def test_sync_commits_metadata_before_waiting_for_issue_provider(db_factory):
    from pullbox.tasks import metadata_task

    sid = await _create_series(db_factory, comicvine_id=91001)
    svc = _make_metadata_svc([])
    tracking = _TrackingFactory(db_factory)
    commits_after_metadata = []

    async def write_series(session, identifier, **kwargs):
        series = await session.get(Series, sid)
        series.description = "Refreshed metadata"
        await session.flush()
        commits_after_metadata.append(tracking.commit_calls)

    async def check_issue_fetch(session, series_id):
        assert tracking.commit_calls > commits_after_metadata[-1], (
            "Provider wait still holds metadata's write transaction"
        )
        return []

    svc.fetch_series.side_effect = write_series
    svc.fetch_issues_for_series.side_effect = check_issue_fetch
    with _sync_patches(tracking, svc, _make_scheduler()):
        await metadata_task.sync_new_issues()
    assert svc.fetch_series.await_args.kwargs.get("download_cover") is False
    async with db_factory() as session:
        series = await session.get(Series, sid)
        assert series.issue_catalog_last_checked_at is not None


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def restore_db_factory(tmp_path):
    # Cancellation may discard a connection; persisted restore state must survive it.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restore.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _create_series(
    factory: async_sessionmaker[AsyncSession],
    *,
    monitored: bool = True,
    comicvine_id: int | None = 99900,
    issue_count: int = 0,
    status: SeriesStatus = SeriesStatus.CONTINUING,
    metadata_last_refreshed: datetime | None = None,
    issue_catalog_state: IssueCatalogState = IssueCatalogState.COMPLETE,
    issue_catalog_last_synced_at: datetime | None = None,
    issue_catalog_last_checked_at: datetime | None = None,
) -> int:
    """Seed a series, return its ID."""
    async with factory() as session:
        series = Series(
            comicvine_id=comicvine_id,
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            status=status,
            series_type=SeriesType.STANDARD,
            monitored=monitored,
            issue_count=issue_count,
            metadata_last_refreshed=metadata_last_refreshed,
            issue_catalog_state=issue_catalog_state,
            issue_catalog_last_synced_at=issue_catalog_last_synced_at,
            issue_catalog_last_checked_at=issue_catalog_last_checked_at,
        )
        session.add(series)
        await session.commit()
        return series.id


@contextlib.contextmanager
def _sync_patches(
    db_factory: object,
    metadata_svc: AsyncMock,
    scheduler: MagicMock,
) -> Generator[MagicMock, None, None]:
    """Context manager that patches all sync_new_issues dependencies."""
    with (
        patch(f"{_MOD}.get_settings") as mock_settings,
        patch(f"{_MOD}.get_session_factory", return_value=db_factory),
        patch(
            f"{_MOD}.get_comicvine_api_key",
            new_callable=AsyncMock,
            return_value="fake-key",
        ),
        patch(
            f"{_MOD}._create_metadata_service",
            return_value=metadata_svc,
        ),
        patch(f"{_MOD}.get_scheduler", return_value=scheduler),
        patch("pullbox.tasks.metadata_sweep_state.get_scheduler", return_value=scheduler),
    ):
        mock_settings.return_value = MagicMock()
        yield mock_settings


def _make_metadata_svc(
    issue_ids: list[int],
) -> AsyncMock:
    """Create a mock MetadataService with fetch_issues_for_series."""
    svc = AsyncMock()
    svc.fetch_series = AsyncMock()

    async def _mock_fetch(session: AsyncSession, sid: int) -> list[Issue]:
        from sqlalchemy import select

        result = await session.execute(select(Issue).where(Issue.id.in_(issue_ids)))
        return list(result.scalars().all())

    svc.fetch_issues_for_series.side_effect = _mock_fetch
    return svc


def _make_scheduler() -> MagicMock:
    """Create a mock scheduler with _scheduler.add_job."""
    scheduler = MagicMock()
    scheduler._scheduler = MagicMock()
    scheduler._scheduler.add_job = MagicMock()
    return scheduler


class _TrackingFactory:
    """Wrap an async session factory and count commit/rollback calls."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.commit_calls = 0
        self.rollback_calls = 0

    def __call__(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        real_cm = self._factory(*args, **kwargs)
        tracker = self

        class _Wrapper:
            async def __aenter__(self) -> AsyncSession:
                session = await real_cm.__aenter__()
                original_commit = session.commit
                original_rollback = session.rollback

                async def tracked_commit() -> None:
                    tracker.commit_calls += 1
                    await original_commit()

                async def tracked_rollback() -> None:
                    tracker.rollback_calls += 1
                    await original_rollback()

                session.commit = tracked_commit  # type: ignore[method-assign]
                session.rollback = tracked_rollback  # type: ignore[method-assign]
                return session

            async def __aexit__(self, exc_type, exc, tb) -> bool | None:  # type: ignore[no-untyped-def]
                await real_cm.__aexit__(exc_type, exc, tb)
                return None

        return _Wrapper()


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_wanted_issues_trigger_search(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When sync finds new issues AND series is monitored, search is scheduled."""
    series_id = await _create_series(db_factory, monitored=True)

    async with db_factory() as session:
        new_issues = [
            Issue(
                series_id=series_id,
                comicvine_id=50001,
                issue_number=1.0,
                title="Issue #1",
                status=IssueStatus.SKIPPED,
            ),
            Issue(
                series_id=series_id,
                comicvine_id=50002,
                issue_number=2.0,
                title="Issue #2",
                status=IssueStatus.SKIPPED,
            ),
        ]
        for issue in new_issues:
            session.add(issue)
        await session.commit()
        issue_ids = [i.id for i in new_issues]

    mock_metadata_svc = _make_metadata_svc(issue_ids)
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_scheduler._scheduler.add_job.assert_called_once()
    call_kwargs = mock_scheduler._scheduler.add_job.call_args
    assert call_kwargs[1]["trigger"] == "date"
    assert "search_new_" in call_kwargs[1]["id"]


@pytest.mark.asyncio
async def test_no_trigger_if_series_not_monitored(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No search triggered for unmonitored series."""
    series_id = await _create_series(db_factory, monitored=False)

    async with db_factory() as session:
        issue = Issue(
            series_id=series_id,
            comicvine_id=50001,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.SKIPPED,
        )
        session.add(issue)
        await session.commit()
        issue_ids = [issue.id]

    mock_metadata_svc = _make_metadata_svc(issue_ids)
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_scheduler._scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_no_trigger_if_no_new_wanted(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No search triggered when series is unmonitored and new issues stay SKIPPED."""
    series_id = await _create_series(db_factory, monitored=False)

    async with db_factory() as session:
        issue = Issue(
            series_id=series_id,
            comicvine_id=50001,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.SKIPPED,
        )
        session.add(issue)
        await session.commit()
        issue_ids = [issue.id]

    mock_metadata_svc = _make_metadata_svc(issue_ids)
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_scheduler._scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_new_issues_get_monitoring_applied(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """New issues have monitoring criteria applied (SKIPPED → WANTED)."""
    series_id = await _create_series(db_factory, monitored=True)

    async with db_factory() as session:
        issue = Issue(
            series_id=series_id,
            comicvine_id=50001,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.SKIPPED,
        )
        session.add(issue)
        await session.commit()
        issue_id = issue.id

    mock_metadata_svc = _make_metadata_svc([issue_id])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    async with db_factory() as session:
        loaded_issue = await session.get(Issue, issue_id)
        assert loaded_issue is not None
        assert loaded_issue.status == IssueStatus.WANTED


@pytest.mark.asyncio
async def test_sync_new_issues_commits_per_series(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """sync_new_issues should release the write lock after each series."""
    first_id = await _create_series(db_factory, monitored=True, comicvine_id=99001)
    second_id = await _create_series(db_factory, monitored=True, comicvine_id=99002)

    tracking_factory = _TrackingFactory(db_factory)
    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(tracking_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    assert mock_metadata_svc.fetch_issues_for_series.await_count == 2
    called_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_issues_for_series.await_args_list
    }
    assert called_ids == {first_id, second_id}
    assert tracking_factory.commit_calls >= 2


@pytest.mark.asyncio
async def test_sync_new_issues_skips_fresh_series_metadata_refresh_but_syncs_issues(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh series metadata should not block normal issue-list sync."""
    fresh_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99001,
        metadata_last_refreshed=datetime.now(UTC),
    )
    stale_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99002,
        metadata_last_refreshed=datetime.now(UTC) - timedelta(days=60),
    )
    missing_refresh_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99003,
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    refreshed_ids = {call.args[1] for call in mock_metadata_svc.fetch_series.await_args_list}
    issue_synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_issues_for_series.await_args_list
    }
    assert refreshed_ids == {99002, 99003}
    assert issue_synced_ids == {fresh_id, stale_id, missing_refresh_id}


@pytest.mark.asyncio
async def test_sync_new_issues_uses_recent_sync_for_fresh_complete_catalogs(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A recently full-synced complete catalog should avoid a full issue-list fetch."""
    full_synced_at = datetime.now(UTC) - timedelta(days=5)
    recent_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99101,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=full_synced_at,
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_recent_issues_for_series.await_args_list
    }
    assert synced_ids == {recent_id}
    mock_metadata_svc.fetch_issues_for_series.assert_not_awaited()

    async with db_factory() as session:
        series = await session.get(Series, recent_id)
        assert series is not None
        assert series.issue_catalog_last_synced_at == full_synced_at
        assert series.issue_catalog_last_checked_at is not None


@pytest.mark.asyncio
async def test_sync_new_issues_skips_continuing_series_checked_within_24_hours(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Continuing catalogs should use the standard 24h issue-check cadence."""
    now = datetime.now(UTC)
    await _create_series(
        db_factory,
        monitored=False,
        comicvine_id=99102,
        status=SeriesStatus.CONTINUING,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(hours=23),
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_metadata_svc.fetch_issues_for_series.assert_not_awaited()
    mock_metadata_svc.fetch_recent_issues_for_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_new_issues_checks_unknown_series_after_24_hours(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown status uses the same standard cadence as continuing series."""
    now = datetime.now(UTC)
    unknown_id = await _create_series(
        db_factory,
        monitored=False,
        comicvine_id=99103,
        status=SeriesStatus.UNKNOWN,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(hours=25),
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_recent_issues_for_series.await_args_list
    }
    assert synced_ids == {unknown_id}


@pytest.mark.asyncio
async def test_sync_new_issues_checks_ended_monitored_series_every_14_days(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ended monitored catalogs use the two-week issue-check cadence."""
    now = datetime.now(UTC)
    not_due_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99104,
        status=SeriesStatus.ENDED,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(days=13),
    )
    due_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99105,
        status=SeriesStatus.ENDED,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(days=14, minutes=1),
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_recent_issues_for_series.await_args_list
    }
    assert synced_ids == {due_id}
    assert not_due_id not in synced_ids


@pytest.mark.asyncio
async def test_sync_new_issues_ended_unmonitored_takes_30_day_precedence(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ended status wins over unmonitored, so ended+unmonitored checks every 30 days."""
    now = datetime.now(UTC)
    not_due_id = await _create_series(
        db_factory,
        monitored=False,
        comicvine_id=99106,
        status=SeriesStatus.ENDED,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(days=29),
    )
    due_id = await _create_series(
        db_factory,
        monitored=False,
        comicvine_id=99107,
        status=SeriesStatus.ENDED,
        metadata_last_refreshed=now,
        issue_catalog_last_synced_at=now - timedelta(days=5),
        issue_catalog_last_checked_at=now - timedelta(days=30, minutes=1),
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_recent_issues_for_series.await_args_list
    }
    assert synced_ids == {due_id}
    assert not_due_id not in synced_ids


@pytest.mark.asyncio
async def test_sync_new_issues_retries_incomplete_catalogs_regardless_of_checked_cadence(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Partial/hydrating/failed catalogs are retry states and bypass normal cadence."""
    now = datetime.now(UTC)
    retry_ids = {
        await _create_series(
            db_factory,
            monitored=False,
            comicvine_id=99108,
            metadata_last_refreshed=now,
            issue_catalog_state=IssueCatalogState.PARTIAL,
            issue_catalog_last_synced_at=now,
            issue_catalog_last_checked_at=now,
        ),
        await _create_series(
            db_factory,
            monitored=False,
            comicvine_id=99109,
            metadata_last_refreshed=now,
            issue_catalog_state=IssueCatalogState.HYDRATING,
            issue_catalog_last_synced_at=now,
            issue_catalog_last_checked_at=now,
        ),
        await _create_series(
            db_factory,
            monitored=False,
            comicvine_id=99110,
            metadata_last_refreshed=now,
            issue_catalog_state=IssueCatalogState.FAILED,
            issue_catalog_last_synced_at=now,
            issue_catalog_last_checked_at=now,
        ),
    }

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    full_synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_issues_for_series.await_args_list
    }
    assert full_synced_ids == retry_ids
    mock_metadata_svc.fetch_recent_issues_for_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_new_issues_full_syncs_stale_and_incomplete_catalogs(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stale or incomplete issue catalogs should still get the full issue-list path."""
    stale_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99111,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=datetime.now(UTC) - timedelta(days=60),
    )
    partial_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99112,
        issue_catalog_state=IssueCatalogState.PARTIAL,
        issue_catalog_last_synced_at=datetime.now(UTC) - timedelta(days=5),
    )
    never_synced_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99113,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=None,
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    full_synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_issues_for_series.await_args_list
    }
    assert full_synced_ids == {stale_id, partial_id, never_synced_id}
    mock_metadata_svc.fetch_recent_issues_for_series.assert_not_awaited()

    async with db_factory() as session:
        never_synced = await session.get(Series, never_synced_id)
        assert never_synced is not None
        assert never_synced.issue_catalog_state == IssueCatalogState.COMPLETE
        assert never_synced.issue_catalog_last_synced_at is not None
        assert never_synced.issue_catalog_last_checked_at is not None


@pytest.mark.asyncio
async def test_sync_new_issues_keeps_unmonitored_series_in_issue_sync(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unmonitored series still participate in catalog sync so future monitoring is warm."""
    unmonitored_id = await _create_series(
        db_factory,
        monitored=False,
        comicvine_id=99121,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=datetime.now(UTC) - timedelta(days=5),
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    synced_ids = {
        call.args[1] for call in mock_metadata_svc.fetch_recent_issues_for_series.await_args_list
    }
    assert synced_ids == {unmonitored_id}
    mock_scheduler._scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_sync_new_issues_skips_series_without_comicvine_id(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Local-only series should not be treated as failed ComicVine sync work."""
    await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=None,
    )

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler):
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_metadata_svc.fetch_series.assert_not_awaited()
    mock_metadata_svc.fetch_issues_for_series.assert_not_awaited()
    mock_metadata_svc.fetch_recent_issues_for_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_new_issues_bootstraps_complete_null_timestamp_catalog_from_local_count(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Complete legacy catalogs with matching local issue counts can avoid first-run full sync."""
    series_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99131,
        issue_count=2,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=None,
    )
    async with db_factory() as session:
        session.add_all(
            [
                Issue(series_id=series_id, comicvine_id=9913101, issue_number=1.0),
                Issue(series_id=series_id, comicvine_id=9913102, issue_number=2.0),
            ]
        )
        await session.commit()

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_metadata_svc.fetch_recent_issues_for_series.assert_awaited_once()
    mock_metadata_svc.fetch_issues_for_series.assert_not_awaited()
    async with db_factory() as session:
        series = await session.get(Series, series_id)
        assert series is not None
        assert series.issue_catalog_last_synced_at is not None
        assert series.issue_catalog_last_checked_at is not None


@pytest.mark.asyncio
async def test_sync_new_issues_full_syncs_null_timestamp_catalog_when_local_count_is_short(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy complete catalogs still full-sync when local issue rows do not match issue_count."""
    series_id = await _create_series(
        db_factory,
        monitored=True,
        comicvine_id=99132,
        issue_count=2,
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_catalog_last_synced_at=None,
    )
    async with db_factory() as session:
        session.add(Issue(series_id=series_id, comicvine_id=9913201, issue_number=1.0))
        await session.commit()

    mock_metadata_svc = AsyncMock()
    mock_metadata_svc.fetch_series = AsyncMock()
    mock_metadata_svc.fetch_issues_for_series = AsyncMock(return_value=[])
    mock_metadata_svc.fetch_recent_issues_for_series = AsyncMock(return_value=[])
    mock_scheduler = _make_scheduler()

    with _sync_patches(db_factory, mock_metadata_svc, mock_scheduler) as mock_settings:
        mock_settings.return_value.metadata_refresh_days = 30
        from pullbox.tasks.metadata_task import sync_new_issues

        await sync_new_issues()

    mock_metadata_svc.fetch_issues_for_series.assert_awaited_once()
    mock_metadata_svc.fetch_recent_issues_for_series.assert_not_awaited()
