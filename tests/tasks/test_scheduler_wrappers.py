"""Direct coverage for thinner scheduler-managed task wrappers."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.scheduler import get_registered_tasks
from pullbox.models import Base
from pullbox.models.blocklist import BlocklistEntry, BlocklistReason, normalize_release_title
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.health import HealthCheckResult, HealthStatus
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.services.update_check import UpdateCheckResult
from pullbox.tasks.backup_task import run_backups
from pullbox.tasks.blocklist_task import expire_blocklist_entries
from pullbox.tasks.cover_backfill_task import scheduled_backfill_series_covers
from pullbox.tasks.download_scheduler_task import (
    scheduled_monitor_downloads,
    scheduled_process_completed,
)
from pullbox.tasks.health_task import (
    cleanup_health_history,
    run_comicvine_health_check,
    run_database_health_check,
    run_download_client_health_checks,
    run_filesystem_health_check,
    run_health_checks,
    run_indexer_health_checks,
    run_scheduler_health_check,
    run_system_health_check,
)
from pullbox.tasks.metadata_scheduler_task import (
    scheduled_refresh_metadata,
    scheduled_sync_new_issues,
)
from pullbox.tasks.metadata_task import refresh_metadata
from pullbox.tasks.scan_task import cleanup_history
from pullbox.tasks.search_scheduler_task import (
    scheduled_purge_search_logs,
    scheduled_search_wanted,
)
from pullbox.tasks.update_check_task import check_for_updates

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-scheduler-wrappers")


@pytest.fixture
async def db_factory():
    """Create an isolated async DB factory for scheduler wrapper tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_issue(factory: async_sessionmaker[AsyncSession], *, title: str = "Batman") -> int:
    """Create a simple monitored issue for download-history tests."""
    async with factory() as session:
        series = Series(
            title=title,
            sort_title=title.lower(),
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
        )
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=1.0, status=IssueStatus.WANTED)
        session.add(issue)
        await session.flush()
        await session.commit()
        return issue.id


class TestCleanupHistoryWrapper:
    """The scheduled history cleanup should prune only old terminal download rows."""

    @pytest.mark.asyncio
    async def test_cleanup_history_prunes_old_completed_and_failed_rows(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        old_completed_at = datetime.now(UTC) - timedelta(days=60)
        recent_completed_at = datetime.now(UTC) - timedelta(days=5)

        async with db_factory() as session:
            session.add_all(
                [
                    DownloadHistory(
                        issue_id=issue_id,
                        title="old-complete",
                        download_url="https://example.com/old-complete.nzb",
                        download_client=DownloadClientType.SABNZBD,
                        state=DownloadState.COMPLETED,
                        completed_at=old_completed_at,
                    ),
                    DownloadHistory(
                        issue_id=issue_id,
                        title="old-failed",
                        download_url="https://example.com/old-failed.nzb",
                        download_client=DownloadClientType.SABNZBD,
                        state=DownloadState.FAILED,
                        completed_at=old_completed_at,
                    ),
                    DownloadHistory(
                        issue_id=issue_id,
                        title="old-downloading",
                        download_url="https://example.com/old-downloading.nzb",
                        download_client=DownloadClientType.SABNZBD,
                        state=DownloadState.DOWNLOADING,
                        completed_at=old_completed_at,
                    ),
                    DownloadHistory(
                        issue_id=issue_id,
                        title="recent-complete",
                        download_url="https://example.com/recent-complete.nzb",
                        download_client=DownloadClientType.SABNZBD,
                        state=DownloadState.COMPLETED,
                        completed_at=recent_completed_at,
                    ),
                ]
            )
            await session.commit()

        monkeypatch.setattr("pullbox.tasks.scan_task.get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            "pullbox.tasks.scan_task.get_settings",
            lambda: SimpleNamespace(history_retention_days=30),
        )

        await cleanup_history()

        async with db_factory() as session:
            remaining_titles = list(
                (
                    await session.execute(
                        select(DownloadHistory.title).order_by(DownloadHistory.title.asc())
                    )
                )
                .scalars()
                .all()
            )

        assert remaining_titles == ["old-downloading", "recent-complete"]


class TestDownloadSchedulerWrappers:
    """The download scheduler wrappers should delegate straight to the helper module."""

    @pytest.mark.asyncio
    async def test_scheduled_monitor_downloads_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.download_scheduler_task.monitor_downloads", helper)

        await scheduled_monitor_downloads()

        helper.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_scheduled_process_completed_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.download_scheduler_task.process_completed", helper)

        await scheduled_process_completed()

        helper.assert_awaited_once_with()


class TestExpireBlocklistWrapper:
    """The scheduled blocklist expiry wrapper should commit real cleanup work."""

    @pytest.mark.asyncio
    async def test_expire_blocklist_entries_prunes_expired_rows(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_created_at = datetime.now(UTC) - timedelta(days=120)
        recent_created_at = datetime.now(UTC) - timedelta(days=10)

        async with db_factory() as session:
            session.add(SystemConfig(key="blocklist.expiry_days", value="90"))
            session.add_all(
                [
                    BlocklistEntry(
                        release_title="Old Release",
                        release_title_normalized=normalize_release_title("Old Release"),
                        reason=BlocklistReason.MANUAL,
                        created_at=old_created_at,
                    ),
                    BlocklistEntry(
                        release_title="Recent Release",
                        release_title_normalized=normalize_release_title("Recent Release"),
                        reason=BlocklistReason.MANUAL,
                        created_at=recent_created_at,
                    ),
                ]
            )
            await session.commit()

        monkeypatch.setattr("pullbox.tasks.blocklist_task.get_session_factory", lambda: db_factory)

        removed = await expire_blocklist_entries()
        assert removed == 1

        async with db_factory() as session:
            remaining_titles = list(
                (await session.execute(select(BlocklistEntry.release_title))).scalars().all()
            )

        assert remaining_titles == ["Recent Release"]


class TestUpdateCheckWrapper:
    """The scheduled update-check wrapper should drive the service correctly."""

    def test_check_for_updates_uses_extended_misfire_grace(self) -> None:
        """The daily update check should tolerate delayed app startup."""
        task = next(t for t in get_registered_tasks() if t.task_id == "check_for_updates")
        assert task.trigger_kwargs["misfire_grace_time"] == 3600

    @pytest.mark.asyncio
    async def test_check_for_updates_forces_service_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeUpdateCheckService:
            def __init__(self) -> None:
                self.check_for_update = AsyncMock(
                    return_value=UpdateCheckResult(
                        current_version="1.0.0",
                        latest_version="1.0.0",
                        update_available=False,
                        checked_at=datetime.now(UTC),
                    )
                )

        service = FakeUpdateCheckService()
        monkeypatch.setattr("pullbox.app.get_update_check_service", lambda: service)
        monkeypatch.setattr(
            "pullbox.services.update_check.UpdateCheckService",
            FakeUpdateCheckService,
        )

        await check_for_updates()

        service.check_for_update.assert_awaited_once_with(force=True)

    @pytest.mark.asyncio
    async def test_check_for_updates_skips_when_service_is_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeUpdateCheckService:
            pass

        monkeypatch.setattr("pullbox.app.get_update_check_service", lambda: object())
        monkeypatch.setattr(
            "pullbox.services.update_check.UpdateCheckService",
            FakeUpdateCheckService,
        )

        await check_for_updates()

    @pytest.mark.asyncio
    async def test_check_for_updates_logs_when_an_update_is_available(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeUpdateCheckService:
            def __init__(self) -> None:
                self.check_for_update = AsyncMock(
                    return_value=UpdateCheckResult(
                        current_version="1.0.0",
                        latest_version="1.1.0",
                        update_available=True,
                        checked_at=datetime.now(UTC),
                        release_url="https://example.com/release",
                    )
                )

        service = FakeUpdateCheckService()
        monkeypatch.setattr("pullbox.app.get_update_check_service", lambda: service)
        monkeypatch.setattr(
            "pullbox.services.update_check.UpdateCheckService",
            FakeUpdateCheckService,
        )

        await check_for_updates()

        service.check_for_update.assert_awaited_once_with(force=True)

    @pytest.mark.asyncio
    async def test_check_for_updates_warns_when_service_returns_no_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeUpdateCheckService:
            def __init__(self) -> None:
                self.check_for_update = AsyncMock(return_value=None)

        service = FakeUpdateCheckService()
        monkeypatch.setattr("pullbox.app.get_update_check_service", lambda: service)
        monkeypatch.setattr(
            "pullbox.services.update_check.UpdateCheckService",
            FakeUpdateCheckService,
        )

        await check_for_updates()

        service.check_for_update.assert_awaited_once_with(force=True)


class TestRefreshMetadataWrapper:
    """The metadata refresh wrapper should use short per-series transactions."""

    @pytest.mark.asyncio
    async def test_refresh_metadata_commits_each_series_and_continues_after_failures(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale_time = datetime.now(UTC) - timedelta(days=30)
        refreshed_time = datetime.now(UTC)

        async with db_factory() as session:
            series_one = Series(
                title="Success Series",
                sort_title="success series",
                comicvine_id=101,
                monitored=True,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                metadata_last_refreshed=stale_time,
            )
            series_two = Series(
                title="Fail Series",
                sort_title="fail series",
                comicvine_id=202,
                monitored=True,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                metadata_last_refreshed=stale_time,
            )
            fresh_series = Series(
                title="Fresh Series",
                sort_title="fresh series",
                comicvine_id=303,
                monitored=True,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                metadata_last_refreshed=datetime.now(UTC),
            )
            session.add_all([series_one, series_two, fresh_series])
            await session.commit()
            success_id = series_one.id
            failing_id = series_two.id
            fresh_id = fresh_series.id

        async def _refresh_series(session: AsyncSession, series_id: int, **kwargs: object) -> None:
            series = await session.get(Series, series_id)
            assert series is not None
            if series_id == failing_id:
                raise RuntimeError("boom")
            series.metadata_last_refreshed = refreshed_time

        fake_service = SimpleNamespace(refresh_series=AsyncMock(side_effect=_refresh_series))

        monkeypatch.setattr("pullbox.tasks.metadata_task.get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            "pullbox.tasks.metadata_task.get_settings",
            lambda: SimpleNamespace(metadata_refresh_days=7, comicvine_rate_limit=1),
        )
        monkeypatch.setattr(
            "pullbox.tasks.metadata_task.get_comicvine_api_key",
            AsyncMock(return_value="cv-key"),
        )
        monkeypatch.setattr(
            "pullbox.tasks.metadata_task._create_metadata_service",
            AsyncMock(return_value=fake_service),
        )

        await refresh_metadata()

        async with db_factory() as session:
            success_series = await session.get(Series, success_id)
            failing_series = await session.get(Series, failing_id)
            fresh_series = await session.get(Series, fresh_id)

        assert success_series is not None
        assert success_series.metadata_last_refreshed == refreshed_time
        assert failing_series is not None
        assert failing_series.metadata_last_refreshed == stale_time
        assert fresh_series is not None
        assert fresh_series.metadata_last_refreshed != refreshed_time
        assert fake_service.refresh_series.await_count == 2


class TestMetadataSchedulerWrappers:
    """The metadata scheduler wrappers should delegate to the helper module."""

    def test_daily_metadata_and_cover_tasks_use_fixed_cron_schedules(self) -> None:
        """Long daily jobs should not reset their next run on every restart."""
        registered = {task.task_id: task for task in get_registered_tasks()}

        assert run_backups.__name__ == "run_backups"
        assert registered["sync_new_issues"].trigger == "cron"
        assert registered["sync_new_issues"].trigger_kwargs == {"hour": 1, "minute": 0}
        assert registered["backfill_series_covers"].trigger == "cron"
        assert registered["backfill_series_covers"].trigger_kwargs == {"hour": 2, "minute": 0}
        assert registered["run_backups"].trigger == "cron"
        assert registered["run_backups"].trigger_kwargs == {"hour": 3}
        assert registered["refresh_metadata"].trigger == "cron"
        assert registered["refresh_metadata"].trigger_kwargs == {"hour": 3, "minute": 15}
        assert (
            registered["refresh_metadata"].trigger_kwargs
            != registered["run_backups"].trigger_kwargs
        )

    def test_app_startup_does_not_override_sync_new_issues_to_interval(self) -> None:
        """The fixed cron schedule should not be replaced by interval-style kwargs."""
        app_source = Path("src/pullbox/app.py").read_text()

        assert '"sync_new_issues": {"hours"' not in app_source

    @pytest.mark.asyncio
    async def test_scheduled_sync_new_issues_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.metadata_scheduler_task.sync_new_issues", helper)

        await scheduled_sync_new_issues()

        helper.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_scheduled_backfill_series_covers_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.cover_backfill_task.backfill_series_covers", helper)

        await scheduled_backfill_series_covers()

        helper.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_scheduled_refresh_metadata_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.metadata_scheduler_task.refresh_metadata", helper)

        await scheduled_refresh_metadata()

        helper.assert_awaited_once_with()


class TestHealthCheckWrapper:
    """The health wrapper should invoke the shared runtime refresher and summarize outcomes."""

    def test_health_tasks_use_component_specific_cadences(self) -> None:
        """Health checks should no longer all run every five minutes."""
        registered = {task.task_id: task for task in get_registered_tasks()}

        assert registered["run_scheduler_health_check"].trigger_kwargs == {"minutes": 30}
        assert registered["run_database_health_check"].trigger_kwargs == {"minutes": 15}
        assert registered["run_filesystem_health_check"].trigger_kwargs == {"minutes": 15}
        assert registered["run_system_health_check"].trigger_kwargs == {"minutes": 15}
        assert registered["run_download_client_health_checks"].trigger_kwargs == {"hours": 4}
        assert registered["run_indexer_health_checks"].trigger_kwargs == {"hours": 8}
        assert registered["run_comicvine_health_check"].trigger_kwargs == {"hours": 8}
        assert registered["cleanup_health_history"].trigger_kwargs == {"hour": 4}

    @pytest.mark.asyncio
    async def test_run_health_checks_invokes_runtime_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outcomes = [
            SimpleNamespace(status=HealthStatus.HEALTHY),
            SimpleNamespace(status=HealthStatus.DEGRADED),
            SimpleNamespace(status=HealthStatus.UNHEALTHY),
        ]
        refresh = AsyncMock(return_value=outcomes)
        monkeypatch.setattr("pullbox.tasks.health_task.run_health_refresh", refresh)

        await run_health_checks()

        refresh.assert_awaited_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("wrapper", "component"),
        [
            (run_scheduler_health_check, "scheduler"),
            (run_database_health_check, "database"),
            (run_filesystem_health_check, "filesystem"),
            (run_system_health_check, "system"),
            (run_download_client_health_checks, "download_clients"),
            (run_indexer_health_checks, "indexers"),
            (run_comicvine_health_check, "comicvine"),
        ],
    )
    async def test_component_health_wrappers_refresh_only_one_component(
        self,
        wrapper,
        component: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        refresh = AsyncMock(return_value=[SimpleNamespace(status=HealthStatus.HEALTHY)])
        monkeypatch.setattr("pullbox.tasks.health_task.run_health_refresh", refresh)

        await wrapper()

        refresh.assert_awaited_once_with(component=component)

    @pytest.mark.asyncio
    async def test_cleanup_health_history_prunes_using_system_config(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        async with db_factory() as session:
            session.add(SystemConfig(key="health_history_retention_days", value="1"))
            session.add_all(
                [
                    HealthCheckResult(
                        component="database",
                        check_name="connectivity",
                        status=HealthStatus.HEALTHY,
                        message="old",
                        checked_at=now - timedelta(days=2),
                    ),
                    HealthCheckResult(
                        component="database",
                        check_name="connectivity",
                        status=HealthStatus.HEALTHY,
                        message="recent",
                        checked_at=now - timedelta(hours=12),
                    ),
                ]
            )
            await session.commit()

        monkeypatch.setattr("pullbox.tasks.health_task.get_session_factory", lambda: db_factory)

        await cleanup_health_history()

        async with db_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(HealthCheckResult.message).order_by(HealthCheckResult.message)
                    )
                )
                .scalars()
                .all()
            )

        assert rows == ["recent"]


class TestSearchSchedulerWrappers:
    """The search scheduler wrappers should delegate to the helper module."""

    @pytest.mark.asyncio
    async def test_scheduled_search_wanted_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.search_scheduler_task.search_wanted", helper)

        await scheduled_search_wanted()

        helper.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_scheduled_purge_search_logs_delegates_to_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        helper = AsyncMock()
        monkeypatch.setattr("pullbox.tasks.search_scheduler_task.purge_search_logs", helper)

        await scheduled_purge_search_logs()

        helper.assert_awaited_once_with()
