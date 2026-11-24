"""Tests for the dashboard intelligence service."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from pullbox.models.dashboard import DashboardMetricRollup, DashboardStorageSnapshot
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.health import HealthCheckResult, HealthCurrentStatus, HealthStatus
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus
from pullbox.services.dashboard_intelligence_service import DashboardIntelligenceService


def _fixed_now() -> datetime:
    return datetime(2026, 4, 8, 12, 0, tzinfo=UTC)


async def _seed_dashboard_fixture(db_session) -> None:  # type: ignore[no-untyped-def]
    now = _fixed_now()

    library_root = LibraryRoot(name="Primary", path="/library", enabled=True)
    db_session.add(library_root)
    await db_session.flush()

    series = Series(
        title="Batman",
        sort_title="Batman",
        monitored=True,
        status=SeriesStatus.CONTINUING,
        issue_count=12,
        library_root_id=library_root.id,
    )
    db_session.add(series)
    await db_session.flush()

    urgent_one = Issue(
        series_id=series.id,
        issue_number=1.0,
        title="Batman #1",
        status=IssueStatus.WANTED,
        release_date=date(2026, 4, 9),
    )
    urgent_two = Issue(
        series_id=series.id,
        issue_number=2.0,
        title="Batman #2",
        status=IssueStatus.WANTED,
        release_date=date(2026, 4, 10),
    )
    next_week = Issue(
        series_id=series.id,
        issue_number=3.0,
        title="Batman #3",
        status=IssueStatus.WANTED,
        release_date=date(2026, 4, 13),
    )
    safe_owned = Issue(
        series_id=series.id,
        issue_number=4.0,
        title="Batman #4",
        status=IssueStatus.OWNED,
        release_date=date(2026, 4, 9),
    )
    db_session.add_all([urgent_one, urgent_two, next_week, safe_owned])
    await db_session.flush()

    current_downloads = [
        DownloadHistory(
            issue_id=urgent_one.id,
            title="Batman 001.cbz",
            download_url="https://example.com/1",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=1, hours=1),
            imported_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        ),
        DownloadHistory(
            issue_id=urgent_two.id,
            title="Batman 002.cbz",
            download_url="https://example.com/2",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=2, hours=1),
            imported_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman 003.cbz",
            download_url="https://example.com/3",
            download_client=DownloadClientType.TRANSMISSION,
            state=DownloadState.COMPLETED,
            completed_at=now - timedelta(days=3),
            updated_at=now - timedelta(days=3),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman 003 retry A.cbz",
            download_url="https://example.com/4",
            download_client=DownloadClientType.TRANSMISSION,
            state=DownloadState.FAILED,
            error_message="Tracker auth failed",
            completed_at=now - timedelta(days=1, hours=4),
            updated_at=now - timedelta(days=1, hours=4),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman 003 retry B.cbz",
            download_url="https://example.com/5",
            download_client=DownloadClientType.TRANSMISSION,
            state=DownloadState.FAILED,
            error_message="Tracker auth failed",
            completed_at=now - timedelta(days=1, hours=3),
            updated_at=now - timedelta(days=1, hours=3),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman active.cbz",
            download_url="https://example.com/6",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.DOWNLOADING,
            updated_at=now - timedelta(minutes=15),
        ),
    ]
    previous_downloads = [
        DownloadHistory(
            issue_id=urgent_one.id,
            title="Batman old 001.cbz",
            download_url="https://example.com/7",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=8, hours=1),
            imported_at=now - timedelta(days=8),
            updated_at=now - timedelta(days=8),
        ),
        DownloadHistory(
            issue_id=urgent_two.id,
            title="Batman old 002.cbz",
            download_url="https://example.com/8",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=9, hours=1),
            imported_at=now - timedelta(days=9),
            updated_at=now - timedelta(days=9),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman old 003.cbz",
            download_url="https://example.com/9",
            download_client=DownloadClientType.TRANSMISSION,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=10, hours=1),
            imported_at=now - timedelta(days=10),
            updated_at=now - timedelta(days=10),
        ),
        DownloadHistory(
            issue_id=next_week.id,
            title="Batman old 004.cbz",
            download_url="https://example.com/10",
            download_client=DownloadClientType.TRANSMISSION,
            state=DownloadState.IMPORTED,
            completed_at=now - timedelta(days=11, hours=1),
            imported_at=now - timedelta(days=11),
            updated_at=now - timedelta(days=11),
        ),
    ]
    db_session.add_all(current_downloads + previous_downloads)

    pending = PendingMatch(
        issue_id=urgent_one.id,
        release_title="Batman candidate A.cbz",
        download_url="https://example.com/pending-a",
        confidence="medium",
        status=PendingMatchStatus.PENDING,
    )
    pending.created_at = now - timedelta(days=6)
    suggestion = MatchingSuggestion(
        library_file_id=0,  # patched after file flush below
        suggested_title="Batman Annual",
        suggested_year=2024,
        confidence_score=0.84,
        status=SuggestionStatus.PENDING,
    )

    unmatched_a = LibraryFile(
        file_path="/library/unmatched/batman-annual-1.cbz",
        file_name="Batman Annual 001.cbz",
        file_size=100_000_000,
        file_format=FileFormat.CBZ,
        file_modified_at=now - timedelta(days=8),
        match_confidence=MatchConfidence.UNMATCHED,
        parsed_series="Batman Annual",
        parsed_issue_number=1.0,
        library_root_id=library_root.id,
    )
    unmatched_a.created_at = now - timedelta(days=8)
    unmatched_b = LibraryFile(
        file_path="/library/unmatched/batman-annual-2.cbz",
        file_name="Batman Annual 002.cbz",
        file_size=100_000_000,
        file_format=FileFormat.CBZ,
        file_modified_at=now - timedelta(days=7),
        match_confidence=MatchConfidence.UNMATCHED,
        parsed_series="Batman Annual",
        parsed_issue_number=2.0,
        library_root_id=library_root.id,
    )
    unmatched_b.created_at = now - timedelta(days=7)
    unmatched_c = LibraryFile(
        file_path="/library/unmatched/saga-special.cbz",
        file_name="Saga Special.cbz",
        file_size=100_000_000,
        file_format=FileFormat.CBZ,
        file_modified_at=now - timedelta(days=5),
        match_confidence=MatchConfidence.UNMATCHED,
        parsed_series="Saga Special",
        parsed_issue_number=1.0,
        library_root_id=library_root.id,
    )
    unmatched_c.created_at = now - timedelta(days=5)
    db_session.add_all([pending, unmatched_a, unmatched_b, unmatched_c])
    await db_session.flush()

    suggestion.library_file_id = unmatched_a.id
    suggestion.created_at = now - timedelta(days=4)
    db_session.add(suggestion)

    current_searches = [
        SearchLog(
            issue_id=urgent_one.id,
            series_title="Batman",
            issue_number=1.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=2,
            results_queued=0,
            results_rejected=8,
            best_confidence="high",
            created_at=now - timedelta(days=1),
        ),
        SearchLog(
            issue_id=urgent_two.id,
            series_title="Batman",
            issue_number=2.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=0,
            results_queued=2,
            results_rejected=8,
            best_confidence="medium",
            created_at=now - timedelta(days=2),
        ),
        SearchLog(
            issue_id=next_week.id,
            series_title="Batman",
            issue_number=3.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=2,
            results_queued=0,
            results_rejected=8,
            best_confidence="high",
            created_at=now - timedelta(days=3),
        ),
        SearchLog(
            issue_id=next_week.id,
            series_title="Batman",
            issue_number=3.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=0,
            results_queued=2,
            results_rejected=8,
            best_confidence="medium",
            created_at=now - timedelta(days=4),
        ),
    ]
    previous_searches = [
        SearchLog(
            issue_id=urgent_one.id,
            series_title="Batman",
            issue_number=1.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=5,
            results_queued=0,
            results_rejected=5,
            best_confidence="high",
            created_at=now - timedelta(days=8),
        ),
        SearchLog(
            issue_id=urgent_two.id,
            series_title="Batman",
            issue_number=2.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=5,
            results_queued=0,
            results_rejected=5,
            best_confidence="high",
            created_at=now - timedelta(days=9),
        ),
        SearchLog(
            issue_id=next_week.id,
            series_title="Batman",
            issue_number=3.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=5,
            results_queued=0,
            results_rejected=5,
            best_confidence="high",
            created_at=now - timedelta(days=10),
        ),
        SearchLog(
            issue_id=next_week.id,
            series_title="Batman",
            issue_number=3.0,
            search_type=SearchType.AUTOMATED,
            results_found=10,
            results_grabbed=5,
            results_queued=0,
            results_rejected=5,
            best_confidence="high",
            created_at=now - timedelta(days=11),
        ),
    ]
    db_session.add_all(current_searches + previous_searches)

    failed_job_one = ImportJob(
        source_path="/imports/batch-a",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FAILED,
        total_files_failed=3,
        error_message="Conflict burst",
    )
    failed_job_one.updated_at = now - timedelta(days=2)
    failed_job_two = ImportJob(
        source_path="/imports/batch-b",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FAILED,
        total_files_failed=2,
        error_message="Match pass crashed",
    )
    failed_job_two.updated_at = now - timedelta(days=3)
    previous_failed_job = ImportJob(
        source_path="/imports/batch-old",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FAILED,
        total_files_failed=1,
        error_message="Old failure",
    )
    previous_failed_job.updated_at = now - timedelta(days=9)
    db_session.add_all([failed_job_one, failed_job_two, previous_failed_job])

    db_session.add_all(
        [
            HealthCheckResult(
                component="downloads",
                check_name="download-client",
                status=HealthStatus.UNHEALTHY,
                message="Download client auth failed",
                checked_at=now - timedelta(minutes=10),
            ),
            HealthCurrentStatus(
                component="downloads",
                current_key="__summary__",
                check_name="download-client",
                subject_key=None,
                subject_key_norm="",
                status=HealthStatus.UNHEALTHY,
                message="Download client auth failed",
                checked_at=now - timedelta(minutes=10),
                is_summary=True,
            ),
            HealthCheckResult(
                component="search",
                check_name="indexer",
                status=HealthStatus.DEGRADED,
                message="Search response slowed down",
                checked_at=now - timedelta(minutes=12),
            ),
            HealthCurrentStatus(
                component="search",
                current_key="__summary__",
                check_name="indexer",
                subject_key=None,
                subject_key_norm="",
                status=HealthStatus.DEGRADED,
                message="Search response slowed down",
                checked_at=now - timedelta(minutes=12),
                is_summary=True,
            ),
        ]
    )

    bucket_start = now - timedelta(days=7)
    bucket_end = bucket_start + timedelta(hours=1)
    db_session.add_all(
        [
            DashboardMetricRollup(
                metric_key="review_debt_total",
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                value=2.0,
            ),
            DashboardMetricRollup(
                metric_key="release_risk_count",
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                value=1.0,
            ),
            DashboardMetricRollup(
                metric_key="health_problem_count",
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                value=0.0,
            ),
        ]
    )

    db_session.add_all(
        [
            DashboardStorageSnapshot(
                snapshot_date=date(2026, 4, 5),
                source_path="/data",
                total_bytes=1_000,
                used_bytes=660,
                free_bytes=340,
                used_percent=66.0,
            ),
            DashboardStorageSnapshot(
                snapshot_date=date(2026, 4, 6),
                source_path="/data",
                total_bytes=1_000,
                used_bytes=690,
                free_bytes=310,
                used_percent=69.0,
            ),
            DashboardStorageSnapshot(
                snapshot_date=date(2026, 4, 7),
                source_path="/data",
                total_bytes=1_000,
                used_bytes=720,
                free_bytes=280,
                used_percent=72.0,
            ),
        ]
    )

    await db_session.commit()


def _patch_disk_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pullbox.services.dashboard_intelligence_service.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1_000, used=760, free=240),
    )


@pytest.mark.asyncio
class TestBuildDashboard:
    """Behavior-first tests for the executive dashboard briefing."""

    async def test_ranks_critical_risks_ahead_of_trend_noise(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )

        priority_keys = [priority.key for priority in intelligence.priorities]
        assert priority_keys[0] in {"health-degradation", "release-coverage-gap"}
        assert "health-degradation" in priority_keys
        assert "release-coverage-gap" in priority_keys
        if "search-yield-drop" in priority_keys:
            assert priority_keys.index("health-degradation") < priority_keys.index(
                "search-yield-drop"
            )
            assert priority_keys.index("release-coverage-gap") < priority_keys.index(
                "search-yield-drop"
            )
        assert intelligence.briefing.state_label == "Critical"

    async def test_calculates_scorecards_from_operational_fixture(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )
        scorecards = {card.key: card for card in intelligence.scorecards}

        assert scorecards["flow-through"].value_label == "40%"
        assert scorecards["review-debt"].value_label == "5 items"
        assert scorecards["release-risk"].value_label == "2 issues"
        assert scorecards["client-reliability"].value_label == "60%"
        assert "degraded threshold" in scorecards["storage-runway"].value_label.lower()

    async def test_storage_summary_measures_enabled_library_root(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        library_root = tmp_path / "comics"
        library_root.mkdir()
        db_session.add(LibraryRoot(name="Primary", path=str(library_root), enabled=True))
        await db_session.commit()

        measured_paths: list[str] = []

        def _disk_usage(path):  # type: ignore[no-untyped-def]
            measured_paths.append(str(path))
            return SimpleNamespace(total=10_000, used=2_000, free=8_000)

        monkeypatch.setattr(
            "pullbox.services.dashboard_intelligence_service.shutil.disk_usage",
            _disk_usage,
        )

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )

        storage = next(card for card in intelligence.scorecards if card.key == "storage-runway")
        assert measured_paths == [str(library_root)]
        assert storage.value_label == "Collecting baseline"

    async def test_healthy_state_stays_quiet_without_fake_urgency(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "pullbox.services.dashboard_intelligence_service.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=1_000, used=400, free=600),
        )

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )

        assert intelligence.briefing.state_label == "Healthy"
        assert intelligence.briefing.priorities == ()
        assert intelligence.watch_items[0].key == "quiet-watchlist"
        assert intelligence.exceptions[0].key == "quiet-exceptions"

    async def test_watchlist_and_exceptions_only_surface_clusters(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )

        assert intelligence.watch_items[0].key != "quiet-watchlist"
        assert any(item.badge_label == "2 repeats" for item in intelligence.exceptions)
        assert any("unmatched files" in item.title.lower() for item in intelligence.exceptions)

    async def test_low_history_uses_collecting_baseline_copy(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "pullbox.services.dashboard_intelligence_service.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=1_000, used=450, free=550),
        )

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )
        scorecards = {card.key: card for card in intelligence.scorecards}

        assert scorecards["flow-through"].value_label == "Collecting baseline"
        assert scorecards["flow-through"].delta_label == "Collecting baseline"
        assert scorecards["storage-runway"].value_label == "Collecting baseline"
        assert intelligence.is_first_run is True

    async def test_existing_rollups_disable_first_run_state(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )

        assert intelligence.is_first_run is False

    async def test_storage_snapshot_cache_failure_falls_back_to_live_usage(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        original_execute = db_session.execute

        async def _broken_execute(statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            if "dashboard_storage_snapshots" in str(statement):
                raise OperationalError(
                    str(statement),
                    {},
                    Exception("database disk image is malformed"),
                )
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db_session, "execute", _broken_execute)

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now()
        )
        scorecards = {card.key: card for card in intelligence.scorecards}

        assert intelligence.briefing.state_label
        assert scorecards["storage-runway"].value_label == "Collecting baseline"

    async def test_rollup_cache_writes_fail_dark_on_locked_dedicated_session(
        self,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_disk_usage(monkeypatch)
        await _seed_dashboard_fixture(db_session)

        class _LockedSession:
            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

            async def __aenter__(self) -> _LockedSession:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
                return None

            async def scalar(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
                return None

            def add(self, obj) -> None:  # type: ignore[no-untyped-def]
                return None

            async def commit(self) -> None:
                raise OperationalError("COMMIT", {}, Exception("database is locked"))

            async def rollback(self) -> None:
                return None

        class _LockedFactory:
            def __call__(self) -> _LockedSession:
                return _LockedSession()

        sleep = AsyncMock()
        warn = MagicMock()
        monkeypatch.setattr("pullbox.database.get_session_factory", lambda: _LockedFactory())
        monkeypatch.setattr("pullbox.services.dashboard_intelligence_service.asyncio.sleep", sleep)
        monkeypatch.setattr(
            "pullbox.services.dashboard_intelligence_service.log_deduped_warning",
            warn,
        )

        original_rollup_count = await db_session.scalar(
            select(func.count(DashboardMetricRollup.id))
        )

        intelligence = await DashboardIntelligenceService(db_session).build_dashboard(
            now=_fixed_now(),
            allow_rollup_refresh=True,
        )

        rollup_count = await db_session.scalar(select(func.count(DashboardMetricRollup.id)))

        assert intelligence.briefing.state_label
        assert rollup_count == original_rollup_count
        assert sleep.await_count == 2
        cache_keys = {call.kwargs["cache_key"] for call in warn.call_args_list}
        assert cache_keys == {"dashboard_metric_rollups", "dashboard_storage_snapshots"}


class TestDashboardCompatibilityFacades:
    """Pin private dashboard facade seams while focused builders own behavior."""

    async def test_metric_loading_facades_delegate_to_metric_loader(self, db_session) -> None:
        service = DashboardIntelligenceService(db_session)
        current_time = _fixed_now()
        snapshot = object()
        storage = object()
        loader = SimpleNamespace(
            load_snapshot=AsyncMock(return_value=snapshot),
            has_terminal_download_history=AsyncMock(return_value=True),
            ensure_daily_storage_snapshot=AsyncMock(),
            persist_rollups=AsyncMock(),
        )
        service._metric_loader = MagicMock(return_value=loader)  # type: ignore[method-assign]

        assert await service._load_snapshot(current_time) is snapshot
        assert await service._has_terminal_download_history() is True
        await service._ensure_daily_storage_snapshot(storage, current_time)  # type: ignore[arg-type]
        await service._persist_rollups(snapshot, current_time)  # type: ignore[arg-type]

        loader.load_snapshot.assert_awaited_once_with(current_time)
        loader.has_terminal_download_history.assert_awaited_once_with()
        loader.ensure_daily_storage_snapshot.assert_awaited_once_with(storage, current_time)
        loader.persist_rollups.assert_awaited_once_with(snapshot, current_time)

    def test_priority_facades_delegate_to_priority_builder(self, db_session) -> None:
        service = DashboardIntelligenceService(db_session)
        snapshot = object()
        priorities = [object()]
        builder = SimpleNamespace(
            build_priorities=MagicMock(return_value=priorities),
            build_health_priority=MagicMock(return_value=None),
        )
        service._priority_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]

        assert service._build_priorities(snapshot) is priorities  # type: ignore[arg-type]
        assert service._build_health_priority(snapshot) is None  # type: ignore[arg-type]

        builder.build_priorities.assert_called_once_with(snapshot)
        builder.build_health_priority.assert_called_once_with(snapshot)

    def test_presentation_facades_delegate_to_presentation_builder(self, db_session) -> None:
        service = DashboardIntelligenceService(db_session)
        snapshot = object()
        priorities: list[object] = []
        scorecards = (object(),)
        builder = SimpleNamespace(
            build_briefing=MagicMock(return_value="briefing"),
            build_scorecards=MagicMock(return_value=scorecards),
            build_live_pulse=MagicMock(return_value="pulse"),
        )
        service._presentation_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]

        assert service._build_briefing(snapshot, priorities) == "briefing"  # type: ignore[arg-type]
        assert service._build_scorecards(snapshot) is scorecards  # type: ignore[arg-type]
        assert service._build_live_pulse(snapshot) == "pulse"  # type: ignore[arg-type]

        builder.build_briefing.assert_called_once_with(snapshot, priorities)
        builder.build_scorecards.assert_called_once_with(snapshot)
        builder.build_live_pulse.assert_called_once_with(snapshot)
