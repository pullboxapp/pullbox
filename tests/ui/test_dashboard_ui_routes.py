"""Route-contract tests for the mission-control dashboard."""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

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
from pullbox.ui import dashboard_routes

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-dashboard-ui")


async def _seed_dashboard_intelligence(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed enough operational data to render a non-trivial dashboard."""
    now = datetime(2026, 4, 8, 12, 0, tzinfo=UTC)

    async with sec_db() as session:
        library_root = LibraryRoot(name="Primary", path="/library", enabled=True)
        session.add(library_root)
        await session.flush()

        series = Series(
            title="Batman",
            sort_title="Batman",
            monitored=True,
            status=SeriesStatus.CONTINUING,
            issue_count=12,
            library_root_id=library_root.id,
        )
        session.add(series)
        await session.flush()

        issue_one = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Batman #1",
            status=IssueStatus.WANTED,
            release_date=date(2026, 4, 9),
        )
        issue_two = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Batman #2",
            status=IssueStatus.WANTED,
            release_date=date(2026, 4, 10),
        )
        issue_three = Issue(
            series_id=series.id,
            issue_number=3.0,
            title="Batman #3",
            status=IssueStatus.WANTED,
            release_date=date(2026, 4, 13),
        )
        session.add_all([issue_one, issue_two, issue_three])
        await session.flush()

        session.add_all(
            [
                DownloadHistory(
                    title="Batman 001.cbz",
                    state=DownloadState.IMPORTED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/1",
                    issue_id=issue_one.id,
                    completed_at=now - timedelta(days=1, hours=1),
                    imported_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                ),
                DownloadHistory(
                    title="Batman 002.cbz",
                    state=DownloadState.COMPLETED,
                    download_client=DownloadClientType.TRANSMISSION,
                    download_url="https://example.com/2",
                    issue_id=issue_two.id,
                    completed_at=now - timedelta(days=2),
                    updated_at=now - timedelta(days=2),
                ),
                DownloadHistory(
                    title="Batman 003 retry A.cbz",
                    state=DownloadState.FAILED,
                    download_client=DownloadClientType.TRANSMISSION,
                    download_url="https://example.com/3",
                    issue_id=issue_three.id,
                    error_message="Tracker auth failed",
                    completed_at=now - timedelta(days=1, hours=4),
                    updated_at=now - timedelta(days=1, hours=4),
                ),
                DownloadHistory(
                    title="Batman live.cbz",
                    state=DownloadState.DOWNLOADING,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/5",
                    issue_id=issue_three.id,
                    updated_at=now - timedelta(minutes=15),
                ),
            ]
        )

        pending = PendingMatch(
            issue_id=issue_one.id,
            release_title="Batman pending.cbz",
            download_url="https://example.com/pending",
            confidence="medium",
            status=PendingMatchStatus.PENDING,
        )
        pending.created_at = now - timedelta(days=5)
        unmatched = LibraryFile(
            file_path="/library/unmatched/batman-annual.cbz",
            file_name="Batman Annual.cbz",
            file_size=120_000_000,
            file_format=FileFormat.CBZ,
            file_modified_at=now - timedelta(days=5),
            match_confidence=MatchConfidence.UNMATCHED,
            parsed_series="Batman Annual",
            parsed_issue_number=1.0,
            library_root_id=library_root.id,
        )
        unmatched.created_at = now - timedelta(days=6)
        session.add_all([pending, unmatched])
        await session.flush()

        session.add(
            MatchingSuggestion(
                library_file_id=unmatched.id,
                suggested_title="Batman Annual",
                suggested_year=2024,
                confidence_score=0.88,
                status=SuggestionStatus.PENDING,
            )
        )

        session.add(
            SearchLog(
                issue_id=issue_two.id,
                series_title="Batman",
                issue_number=2.0,
                search_type=SearchType.AUTOMATED,
                results_found=10,
                results_grabbed=2,
                results_queued=1,
                results_rejected=2,
                best_confidence="high",
            )
        )
        session.add_all(
            [
                ImportJob(
                    source_path="/tmp/dashboard/import-completed",
                    source_type=ImportSourceType.FILESYSTEM,
                    status=ImportJobStatus.COMPLETED,
                    total_files_imported=18,
                    import_completed_at=now - timedelta(hours=6),
                ),
                ImportJob(
                    source_path="/tmp/dashboard/import-failed",
                    source_type=ImportSourceType.FILESYSTEM,
                    status=ImportJobStatus.FAILED,
                    total_files_failed=2,
                    error_message="Metadata timeout while importing files.",
                    import_completed_at=now - timedelta(hours=10),
                ),
            ]
        )

        session.add_all(
            [
                HealthCheckResult(
                    component="downloads",
                    check_name="download-client",
                    status=HealthStatus.UNHEALTHY,
                    message="Download client auth failed",
                    checked_at=now - timedelta(minutes=10),
                    is_summary=True,
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
            ]
        )

        bucket_start = now - timedelta(days=7)
        session.add_all(
            [
                DashboardMetricRollup(
                    metric_key="review_debt_total",
                    bucket_start=bucket_start,
                    bucket_end=bucket_start + timedelta(hours=1),
                    value=2.0,
                ),
                DashboardMetricRollup(
                    metric_key="release_risk_count",
                    bucket_start=bucket_start,
                    bucket_end=bucket_start + timedelta(hours=1),
                    value=1.0,
                ),
            ]
        )
        session.add(
            DashboardStorageSnapshot(
                snapshot_date=date(2026, 4, 7),
                source_path="/data",
                total_bytes=1_000,
                used_bytes=720,
                free_bytes=280,
                used_percent=72.0,
            )
        )
        await session.commit()


async def _seed_dashboard_alert_only(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed a dashboard state with alerts but no download exceptions."""
    now = datetime(2026, 4, 8, 12, 0, tzinfo=UTC)

    async with sec_db() as session:
        session.add_all(
            [
                HealthCheckResult(
                    component="downloads",
                    check_name="download-client",
                    status=HealthStatus.UNHEALTHY,
                    message="Download client auth failed",
                    checked_at=now - timedelta(minutes=10),
                    is_summary=True,
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
            ]
        )
        await session.commit()


@pytest.mark.asyncio
class TestDashboardRouteContracts:
    """Verify the mission-control dashboard renders stable prototype regions."""

    async def test_dashboard_renders_mission_control_regions(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_dashboard_intelligence(sec_db)

        response = await authenticated_client.get("/")

        assert response.status_code == 200
        assert 'data-testid="dashboard-page"' in response.text
        assert 'data-testid="dashboard-mission-control"' in response.text
        assert 'data-testid="dashboard-mission-summary"' in response.text
        assert 'data-testid="dashboard-mission-title-block"' in response.text
        assert 'data-testid="dashboard-mission-gauges"' in response.text
        assert 'data-testid="dashboard-mission-freshness"' not in response.text
        assert 'data-testid="dashboard-gauge-completion"' in response.text
        assert 'data-testid="dashboard-gauge-wanted"' in response.text
        assert 'data-testid="dashboard-gauge-downloads"' in response.text
        assert 'data-testid="dashboard-gauge-health"' in response.text
        assert 'data-testid="dashboard-scoreboard"' in response.text
        assert 'data-testid="dashboard-alerts"' in response.text
        assert 'data-testid="dashboard-alert-sys-led"' in response.text
        assert "/static/css/tailwind.css?v=" in response.text
        assert "/static/js/pullbox.js?v=" in response.text
        assert 'data-testid="dashboard-download-exceptions"' in response.text
        assert 'data-testid="dashboard-download-exception-sys-led"' in response.text
        assert 'data-testid="dashboard-downloads-panel"' not in response.text
        assert 'data-testid="dashboard-recent-activity"' in response.text
        assert 'data-testid="dashboard-recent-outcomes-table"' in response.text
        assert response.text.count('class="dashboard-table-wrap"') >= 3
        assert response.text.count('class="dashboard-table"') >= 3
        assert "dashboard-activity-card" not in response.text
        assert "dashboard-activity-row" not in response.text
        assert "Recent Outcomes" in response.text
        assert "Automated search found" not in response.text
        assert "hit a download failure" not in response.text
        assert "Import run completed." in response.text
        assert "18 files imported." in response.text
        assert "Import run failed." in response.text
        assert "2 file actions failed." in response.text
        assert 'data-testid="dashboard-footer-dock"' in response.text
        assert 'data-testid="dashboard-footer-strip"' not in response.text
        assert 'data-testid="dashboard-live-pulse"' not in response.text
        assert 'data-testid="dashboard-watchlist"' not in response.text
        assert 'data-testid="dashboard-exceptions"' not in response.text
        assert "Updated just now" not in response.text
        assert "dashboard-mission-control__freshness" not in response.text

    async def test_dashboard_renders_quiet_note_when_everything_is_clear(
        self,
        authenticated_client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(
            "pullbox.services.dashboard_intelligence_service.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=1_000_000_000, used=100_000_000, free=900_000_000),
        )

        response = await authenticated_client.get("/")

        assert response.status_code == 200
        assert 'data-testid="dashboard-page"' in response.text
        assert 'data-testid="dashboard-quiet-note"' in response.text
        assert 'data-testid="dashboard-alerts"' not in response.text
        assert 'data-testid="dashboard-downloads-panel"' not in response.text
        assert 'data-testid="dashboard-download-exceptions"' not in response.text
        assert 'data-testid="dashboard-recent-activity"' in response.text
        assert 'data-testid="dashboard-continue-reading"' not in response.text

    async def test_dashboard_continue_shelf_is_bounded_to_eight_cards(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        from tests.ui.test_reading_ui_routes import _seed_reading_items

        await _seed_reading_items(sec_db, sec_user, count=9, mode="continue")

        response = await authenticated_client.get("/")

        assert response.status_code == 200
        assert 'data-testid="dashboard-continue-reading"' in response.text
        assert response.text.count('data-testid="reading-card"') == 8
        assert "Continue reading" in response.text
        assert 'href="/reading"' in response.text
        assert "Open reading queue" in response.text
        assert 'hx-trigger="every 3s"' not in response.text

    async def test_reader_gate_hides_dashboard_continue_shelf(
        self,
        authenticated_client,
        sec_db,
        sec_user,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from tests.ui.test_reading_ui_routes import _seed_reading_items

        await _seed_reading_items(sec_db, sec_user, count=1, mode="continue")
        monkeypatch.setattr(
            dashboard_routes,
            "get_settings",
            lambda: SimpleNamespace(reader_enabled=False),
            raising=False,
        )

        response = await authenticated_client.get("/")

        assert response.status_code == 200
        assert 'data-testid="dashboard-continue-reading"' not in response.text
        assert 'data-reading-action="want-to-read"' not in response.text
        assert 'data-reading-action="completion"' not in response.text

    async def test_dashboard_storage_strip_measures_enabled_library_root(
        self,
        sec_db,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        library_root = tmp_path / "comics"
        library_root.mkdir()
        async with sec_db() as session:
            session.add(LibraryRoot(name="Primary", path=str(library_root), enabled=True))
            await session.commit()

            measured_paths: list[str] = []

            def _disk_usage(path):  # type: ignore[no-untyped-def]
                measured_paths.append(str(path))
                return SimpleNamespace(total=10_000, used=2_000, free=8_000)

            monkeypatch.setattr(dashboard_routes.shutil, "disk_usage", _disk_usage)
            dashboard = SimpleNamespace(
                scorecards=(SimpleNamespace(key="storage-runway", value_label="8 TB runway"),)
            )

            free_bytes, delta = await dashboard_routes.load_dashboard_storage_strip(
                session,
                dashboard,
            )

        assert measured_paths == [str(library_root)]
        assert free_bytes == 8_000
        assert delta == "8 TB runway"

    async def test_dashboard_shows_download_exception_all_clear_banner_when_needed(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_dashboard_alert_only(sec_db)

        response = await authenticated_client.get("/")

        assert response.status_code == 200
        assert 'data-testid="dashboard-alerts"' in response.text
        assert 'data-testid="dashboard-download-exceptions"' in response.text
        assert 'data-testid="dashboard-download-exception-all-clear"' in response.text
        assert "No download exceptions need attention right now." in response.text

    async def test_dashboard_briefing_partial_returns_fragment_for_htmx(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_dashboard_intelligence(sec_db)

        response = await authenticated_client.get(
            "/htmx/dashboard/briefing",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="dashboard-mission-control"' in response.text
        assert 'data-testid="dashboard-page"' not in response.text

    async def test_dashboard_secondary_partials_return_fragments_for_htmx(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_dashboard_intelligence(sec_db)

        scoreboard_response = await authenticated_client.get(
            "/htmx/dashboard/scoreboard",
            headers={"HX-Request": "true"},
        )
        alerts_response = await authenticated_client.get(
            "/htmx/dashboard/priorities",
            headers={"HX-Request": "true"},
        )
        exceptions_response = await authenticated_client.get(
            "/htmx/dashboard/download-exceptions-panel",
            headers={"HX-Request": "true"},
        )
        recent_response = await authenticated_client.get(
            "/htmx/dashboard/recent-activity",
            headers={"HX-Request": "true"},
        )

        assert scoreboard_response.status_code == 200
        assert 'data-testid="dashboard-scoreboard"' in scoreboard_response.text
        assert 'data-testid="dashboard-page"' not in scoreboard_response.text

        assert alerts_response.status_code == 200
        assert 'data-testid="dashboard-alerts"' in alerts_response.text
        assert 'data-testid="dashboard-page"' not in alerts_response.text

        assert exceptions_response.status_code == 200
        assert 'data-testid="dashboard-download-exceptions"' in exceptions_response.text
        assert 'data-testid="dashboard-page"' not in exceptions_response.text

        assert recent_response.status_code == 200
        assert 'data-testid="dashboard-recent-activity"' in recent_response.text
        assert 'data-testid="dashboard-recent-outcomes-table"' in recent_response.text
        assert 'class="dashboard-table-wrap"' in recent_response.text
        assert 'class="dashboard-table"' in recent_response.text
        assert "dashboard-activity-card" not in recent_response.text
        assert "dashboard-activity-row" not in recent_response.text
        assert "Recent Outcomes" in recent_response.text
        assert "Automated search found" not in recent_response.text
        assert 'data-testid="dashboard-page"' not in recent_response.text
