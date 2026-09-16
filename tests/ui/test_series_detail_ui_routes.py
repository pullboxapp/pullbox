"""Route-contract tests for the series detail page rewrite."""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-series-detail-ui-tests")

_EN_DASH = "\u2013"


def _series_monitor_control_html(html: str) -> str:
    """Return the isolated monitor toggle markup from the rendered series page."""
    start = html.index('data-testid="series-action-monitor-control"')
    end = html.index('data-testid="series-action-search"', start)
    return html[start:end]


def _series_alternate_names_html(html: str) -> str:
    """Return the isolated alternate names panel markup from the rendered series page."""
    start = html.index('data-testid="series-detail-alternate-names"')
    end = html.index('data-testid="series-detail-hero-actions-panel"', start)
    return html[start:end]


def _series_detail_content_html(html: str) -> str:
    """Return page-specific content without shared shell recovery scripts."""
    start = html.index('data-testid="series-detail-page"')
    end = html.index('data-testid="page-footer-dock"', start)
    return html[start:end]


class _SelectRecorder:
    """Capture SELECT statements emitted by the test app engine."""

    def __init__(self, async_engine) -> None:  # type: ignore[no-untyped-def]
        self._engine = async_engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self) -> _SelectRecorder:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)


@pytest.fixture
async def seeded_series_detail_ui_data(sec_db) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Seed a series-detail dataset with issues, related series, and file data."""
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
    from pullbox.models.publisher import Publisher
    from pullbox.models.series import Series, SeriesStatus, SeriesType

    async with sec_db() as session:
        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Series UI Test Library", path="/tmp/series-ui", enabled=True)
        session.add_all([publisher, root])
        await session.flush()

        series_path = Path("/tmp/series-ui/batman")
        series_path.mkdir(parents=True, exist_ok=True)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            issue_count=3,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(series_path),
            description="Batman faces a changing Gotham.",
            comicvine_id=12345,
            comicvine_url="https://comicvine.gamespot.com/batman/4050-12345/",
            alternate_names=["The Bat", "Dark Knight"],
        )
        session.add(series)
        await session.flush()
        series.cover_path = f"/api/v1/series/{series.id}/cover"

        child = Series(
            title="Batman Beyond",
            sort_title="Batman Beyond",
            year_start=2015,
            status=SeriesStatus.ENDED,
            monitored=False,
            issue_count=1,
            publisher_id=publisher.id,
            library_root_id=root.id,
            parent_series_id=series.id,
            series_type=SeriesType.ONE_SHOT,
            path="/tmp/series-ui/batman-beyond",
        )
        session.add(child)
        await session.flush()

        owned_issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="I Am Gotham",
            release_date=date(2016, 6, 1),
            status=IssueStatus.OWNED,
        )
        wanted_issue = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Night of the Monster Men",
            release_date=date(2016, 7, 1),
            status=IssueStatus.WANTED,
        )
        skipped_issue = Issue(
            series_id=series.id,
            issue_number=3.0,
            title="The Rules of Engagement",
            release_date=date(2016, 8, 1),
            status=IssueStatus.SKIPPED,
        )
        session.add_all([owned_issue, wanted_issue, skipped_issue])
        await session.flush()

        session.add(
            LibraryFile(
                issue_id=owned_issue.id,
                library_root_id=root.id,
                file_path="/tmp/series-ui/batman/Batman 001 (2016).cbz",
                file_name="Batman 001 (2016).cbz",
                file_size=52_428_800,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime(2024, 1, 1, tzinfo=UTC),
                match_confidence=MatchConfidence.MANUAL,
            )
        )

        await session.commit()
        return {
            "series_id": series.id,
            "paused_series_id": child.id,
            "owned_issue_id": owned_issue.id,
        }


@pytest.mark.asyncio
class TestSeriesDetailRouteContracts:
    """Verify the server-side series-detail rendering contract."""

    async def test_detail_hero_keeps_summary_beside_cover_for_long_titles(
        self,
    ) -> None:
        input_css = Path("src/pullbox/ui/static/css/input.css").read_text(encoding="utf-8")

        assert ".series-domain-hero-inner" in input_css
        assert "grid-template-columns: 130px minmax(0, 1fr) minmax(13rem, 16rem);" in input_css
        assert "grid-template-columns: 120px minmax(0, 1fr);" in input_css
        assert "grid-template-columns: 104px minmax(0, 1fr);" in input_css
        assert ".series-domain-actions-panel" in input_css
        assert "grid-column: 1 / -1;" in input_css
        assert "overflow-wrap: anywhere;" in input_css

    async def test_full_page_renders_stable_series_shell(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="series-detail-page"' in response.text
        assert 'hx-history="false"' in response.text
        assert 'data-testid="series-detail-back-link"' in response.text
        assert 'data-series-index-link="true"' in response.text
        assert 'href="/series"' in response.text
        assert "Back to series" in response.text
        assert 'data-testid="series-detail-breadcrumbs"' in response.text
        assert 'data-testid="series-detail-hero"' in response.text
        assert 'data-testid="series-detail-cover-link"' in response.text
        expected_cover_prefix = (
            f"/api/v1/series/{seeded_series_detail_ui_data['series_id']}/cover?v="
        )
        assert expected_cover_prefix in response.text
        assert 'data-testid="series-detail-title"' in response.text
        assert 'data-testid="series-detail-title-link"' in response.text
        assert 'href="https://comicvine.gamespot.com/batman/4050-12345/"' in response.text
        assert 'aria-label="Open Batman on ComicVine"' in response.text
        assert 'target="_blank"' in response.text
        assert 'rel="noopener"' in response.text
        assert 'data-testid="series-detail-status-row"' in response.text
        assert "x-text=\"monitored ? 'Monitored' : 'Paused'\"" in response.text
        assert ":class=\"monitored ? 'pill-info' : 'pill-neutral'\"" in response.text
        assert f"2016{_EN_DASH}present" in response.text
        assert ":data-tip=\"monitored ? 'Monitored' : 'Paused'\"" in response.text
        assert 'data-tip="Remove alternate name"' in response.text
        assert 'data-testid="series-detail-hero-summary-panel"' in response.text
        assert 'data-testid="series-detail-hero-actions-panel"' in response.text
        assert 'class="series-domain-actions-card"' not in response.text
        actions_index = response.text.index('data-testid="series-detail-hero-actions-panel"')
        meta_grid_index = response.text.index('data-testid="series-detail-meta-grid"')
        assert actions_index < meta_grid_index
        assert 'data-testid="series-detail-gauge-row"' not in response.text
        assert 'data-testid="series-detail-acquisition-bar"' not in response.text
        assert "series-domain-issues-progress-track" in response.text
        assert "series-domain-issues-progress-fill" in response.text
        assert "series-domain-issues-progress-label" in response.text
        assert 'data-testid="series-detail-meta-grid"' in response.text
        assert 'data-testid="series-detail-alternate-names"' in response.text
        assert 'data-testid="series-detail-alternate-names-list"' in response.text
        alternate_names_html = _series_alternate_names_html(response.text)
        form_index = alternate_names_html.index('class="series-domain-alt-form"')
        list_index = alternate_names_html.index('data-testid="series-detail-alternate-names-list"')
        assert form_index < list_index
        assert "The Bat" in alternate_names_html
        assert "Dark Knight" in alternate_names_html
        assert ">None<" not in alternate_names_html
        assert 'data-testid="series-detail-actions"' in response.text
        assert 'data-testid="series-detail-actions-title"' in response.text
        assert 'data-testid="series-action-monitor-control"' in response.text
        assert 'data-testid="series-action-monitor-label"' in response.text
        monitor_control_html = _series_monitor_control_html(response.text)
        assert ">Monitored</span>" in monitor_control_html
        assert "Toggle monitoring for this series" in monitor_control_html
        assert 'data-testid="series-action-monitor-toggle"' in response.text
        assert ':checked="monitored"' in monitor_control_html
        assert ":aria-checked=\"monitored ? 'true' : 'false'\"" in monitor_control_html
        assert '@change="toggleMonitoring($event.target.checked)"' in monitor_control_html
        assert 'data-testid="series-action-refresh"' in response.text
        assert 'data-testid="series-action-search"' in response.text
        assert 'data-testid="series-action-status-toggle"' in response.text
        assert "Mark as ended" in response.text
        assert '@click="updateStatusOverride(&#34;ended&#34;)"' in response.text
        assert 'data-testid="series-action-status-auto"' not in response.text
        assert 'data-testid="series-action-delete"' in response.text
        assert 'data-testid="series-detail-issues-section"' in response.text
        assert 'data-testid="series-detail-issues-summary"' in response.text
        assert 'data-testid="series-detail-issues-table"' in response.text
        assert 'data-testid="series-detail-issues-status-select"' in response.text
        assert 'data-tip="Search"' in response.text

        assert 'data-tip="Manual search"' in response.text
        assert 'data-testid="series-detail-telemetry-strip"' not in response.text
        assert 'data-testid="series-issue-status-toggle"' not in response.text
        assert 'hx-post="/htmx/issues/' not in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'hx-push-url="false"' in response.text
        assert 'data-testid="series-detail-delete-modal"' in response.text
        assert 'data-delete-modal-contract="series-v1"' in response.text
        assert 'data-testid="series-delete-submit"' in response.text
        assert 'data-testid="series-delete-warning-row"' in response.text
        assert 'data-testid="series-delete-summary"' in response.text
        assert 'data-testid="series-delete-options-header"' in response.text
        assert 'data-testid="series-delete-options-panel"' in response.text
        assert 'data-testid="issue-search-modal"' in response.text
        assert 'data-testid="issue-search-modal-footer-close"' in response.text
        assert 'data-testid="issue-search-modal-stats"' in response.text
        assert 'data-testid="issue-search-modal-footer-meta"' in response.text
        assert 'data-testid="header-donations-button"' in response.text
        assert 'data-testid="header-theme-toggle"' in response.text
        assert 'data-testid="live-updates-toggle"' not in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="app-footer"' not in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert "page-dock-inner page-dock-inner-status-only" in response.text
        assert 'data-testid="page-dock-pagination"' not in response.text
        assert "transition:true" not in response.text
        assert "window.location.reload()" not in _series_detail_content_html(response.text)
        assert "window.__autoSearching" not in response.text
        assert "htmx.ajax('POST', '/htmx/series/" not in response.text

    async def test_series_issue_rows_render_reading_state_and_distinct_series_summary(
        self,
        authenticated_client,
        sec_db,
        sec_user,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.reader import IssueReaderState

        now = datetime.now(UTC)
        issue_id = seeded_series_detail_ui_data["owned_issue_id"]
        async with sec_db() as session:
            session.add(
                IssueReaderState(
                    user_id=sec_user.id,
                    issue_id=issue_id,
                    last_page_index=1,
                    content_revision="series-detail-revision",
                    page_count=5,
                    progress_updated_at=now,
                    last_opened_at=now,
                    want_to_read=True,
                    want_to_read_updated_at=now,
                )
            )
            await session.commit()

        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )

        assert response.status_code == 200
        assert 'Reading<span class="sr-only"> state</span>' in response.text
        assert 'data-testid="series-reading-summary"' in response.text
        assert "Read 0 of 1 readable" in response.text
        assert 'data-testid="series-issue-reading"' in response.text
        assert "Page 2/5" in response.text
        assert 'data-testid="series-issue-read"' in response.text
        assert 'aria-label="Continue Batman #1"' in response.text
        assert 'data-testid="series-issue-reading-menu"' in response.text
        assert "In Want to Read" in response.text
        assert "window.pullboxSeriesIssuesCanPoll()" in response.text

    async def test_series_detail_reader_queries_are_bounded_to_one_map_and_one_aggregate(
        self,
        authenticated_client,
        sec_db,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        engine = sec_db.kw["bind"]
        with _SelectRecorder(engine) as recorder:
            response = await authenticated_client.get(
                f"/series/{seeded_series_detail_ui_data['series_id']}"
            )

        assert response.status_code == 200
        reader_queries = [
            statement for statement in recorder.statements if "issue_reader_states" in statement
        ]
        assert len(reader_queries) == 2
        assert any("issue_reader_states.issue_id IN" in statement for statement in reader_queries)
        assert any("GROUP BY issues.series_id" in statement for statement in reader_queries)

    async def test_series_issue_reading_fragment_refreshes_only_the_changed_row(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        issue_id = seeded_series_detail_ui_data["owned_issue_id"]

        response = await authenticated_client.get(f"/htmx/issues/{issue_id}/reading-row")

        assert response.status_code == 200
        assert response.text.count('data-testid="series-issue-row"') == 1
        assert f'data-reading-refresh-url="/htmx/issues/{issue_id}/reading-row"' in response.text
        assert 'data-testid="series-detail-issues-section"' not in response.text

    async def test_pull_list_origin_changes_the_series_detail_back_link(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        return_url = "/pull-list?filter=missing&search=Batman&sort=-wanted&page=2&per_page=50"
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}",
            params={"from": "pull-list", "return_to": return_url},
        )

        assert response.status_code == 200
        back_link_test_id = response.text.index('data-testid="series-detail-back-link"')
        back_link_start = response.text.rindex("<a", 0, back_link_test_id)
        back_link_end = response.text.index("</a>", back_link_start)
        back_link_html = response.text[back_link_start:back_link_end]
        assert (
            'href="/pull-list?filter=missing&amp;search=Batman&amp;sort=-wanted&amp;page=2&amp;per_page=50"'
            in back_link_html
        )
        assert "Back to pull list" in back_link_html
        assert 'data-series-index-link="true"' not in back_link_html

    async def test_pull_list_origin_rejects_an_external_return_url(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}",
            params={"from": "pull-list", "return_to": "https://example.test/steal"},
        )

        assert response.status_code == 200
        back_link_test_id = response.text.index('data-testid="series-detail-back-link"')
        back_link_start = response.text.rindex("<a", 0, back_link_test_id)
        back_link_end = response.text.index("</a>", back_link_start)
        back_link_html = response.text[back_link_start:back_link_end]
        assert 'href="/pull-list"' in back_link_html

    async def test_unmonitored_series_action_switch_uses_positive_monitoring_semantics(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['paused_series_id']}"
        )

        assert response.status_code == 200
        assert "monitored: false" in response.text
        assert 'data-testid="series-action-monitor-label"' in response.text
        monitor_control_html = _series_monitor_control_html(response.text)
        assert ">Monitored</span>" in monitor_control_html
        assert "Toggle monitoring for this series" in monitor_control_html
        assert "x-text=\"monitored ? 'Monitored' : 'Paused'\"" in response.text
        assert ":class=\"monitored ? 'pill-info' : 'pill-neutral'\"" in response.text
        assert ':checked="monitored"' in monitor_control_html
        assert ":aria-checked=\"monitored ? 'true' : 'false'\"" in monitor_control_html
        assert '@change="toggleMonitoring($event.target.checked)"' in monitor_control_html
        assert "Mark as continuing" in response.text
        assert '@click="updateStatusOverride(&#34;continuing&#34;)"' in response.text

    async def test_monitoring_toggle_refreshes_in_place_without_navigating(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text(encoding="utf-8")

        assert response.status_code == 200
        assert "x-text=\"monitored ? 'Monitored' : 'Paused'\"" in response.text
        assert ":data-tip=\"monitored ? 'Monitored' : 'Paused'\"" in response.text
        monitoring_start = script.index("toggleMonitoring: function (enabled)")
        monitoring_end = script.index("updateStatusOverride: function", monitoring_start)
        monitoring_script = script[monitoring_start:monitoring_end]
        assert "window.location.assign" not in monitoring_script
        assert "refreshIssuesPanel" in monitoring_script
        assert "self.saving = false" in monitoring_script

    async def test_manual_status_override_offers_comicvine_restore_action(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.series import Series, SeriesStatus, SeriesStatusOverride

        async with sec_db() as session:
            series = await session.get(Series, seeded_series_detail_ui_data["series_id"])
            assert series is not None
            series.status = SeriesStatus.ENDED
            series.status_override = SeriesStatusOverride.ENDED
            await session.commit()

        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )

        assert response.status_code == 200
        assert "Mark as continuing" in response.text
        assert 'data-testid="series-action-status-auto"' in response.text
        assert "Use ComicVine status" in response.text
        assert 'class="pill pill-warning">Manual status</span>' in response.text

    async def test_series_detail_status_actions_use_explicit_override_api(
        self,
    ) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text(encoding="utf-8")
        actions = Path("src/pullbox/ui/templates/partials/series_detail_actions.html").read_text(
            encoding="utf-8"
        )

        assert "updateStatusOverride: function (statusOverride)" in script
        assert "status_override: statusOverride" in script
        assert 'data-testid="series-action-status-toggle"' in actions
        assert "updateStatusOverride(null)" in actions

    async def test_alternate_names_empty_state_has_no_none_placeholder(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.library import LibraryRoot
        from pullbox.models.series import Series

        async with sec_db() as session:
            root = LibraryRoot(name="Series UI Test Library", path="/tmp/series-ui", enabled=True)
            session.add(root)
            await session.flush()
            series = Series(
                title="No Alternates",
                sort_title="No Alternates",
                monitored=True,
                library_root_id=root.id,
                alternate_names=[],
            )
            session.add(series)
            await session.commit()
            series_id = series.id

        response = await authenticated_client.get(f"/series/{series_id}")

        assert response.status_code == 200
        alternate_names_html = _series_alternate_names_html(response.text)
        assert 'data-testid="series-detail-alternate-names-list"' in alternate_names_html
        assert ">None<" not in alternate_names_html
        assert 'class="series-domain-alt-form"' in alternate_names_html

    @pytest.mark.parametrize(
        ("catalog_state", "expected_copy"),
        [
            ("hydrating", "Issue catalog sync in progress"),
            ("partial", "Issue catalog is incomplete"),
            ("failed", "Issue catalog sync needs attention"),
        ],
    )
    async def test_series_detail_surfaces_non_complete_issue_catalog_state(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
        sec_db,
        catalog_state: str,
        expected_copy: str,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.series import IssueCatalogState, Series

        async with sec_db() as session:
            series = await session.get(Series, seeded_series_detail_ui_data["series_id"])
            assert series is not None
            series.issue_catalog_state = IssueCatalogState(catalog_state)
            if catalog_state == "failed":
                series.issue_catalog_error = "ComicVine timed out while fetching issues"
            await session.commit()

        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="series-detail-catalog-state-banner"' in response.text
        assert expected_copy in response.text
        assert "issue list and release metadata" in response.text
        if catalog_state in {"partial", "failed"}:
            assert 'data-testid="series-detail-catalog-refresh"' in response.text
            assert "Retry metadata sync" in response.text
        else:
            assert 'data-testid="series-detail-catalog-refresh"' not in response.text

        refresh_start = response.text.index('data-testid="series-action-refresh"')
        refresh_end = response.text.index(
            'data-testid="series-action-status-toggle"',
            refresh_start,
        )
        refresh_html = response.text[refresh_start:refresh_end]
        if catalog_state == "hydrating":
            assert "disabled" in refresh_html
            assert 'aria-disabled="true"' in refresh_html
            assert "Metadata syncing" in refresh_html
            assert "metadataSyncing: true" in response.text
            assert ':disabled="refreshing || metadataSyncing"' in refresh_html
            assert "metadataSyncing ? 'Metadata syncing...' : 'Refresh metadata'" in refresh_html
        else:
            assert 'aria-disabled="true"' not in refresh_html
            assert "Refresh metadata" in refresh_html

    async def test_series_detail_stops_sync_polling_and_reenables_refresh(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text(encoding="utf-8")
        detail_start = script.index("function seriesDetailPage(config)")
        detail_end = script.index("function issueSearchModal", detail_start)
        detail_script = script[detail_start:detail_end]

        assert "metadataSyncing: false" in detail_script
        assert "this.metadataSyncing = !!cfg.metadataSyncing" in detail_script
        assert "startMetadataSyncPolling" in detail_script
        assert 'data.issue_catalog_state !== "hydrating"' in detail_script
        assert "self.metadataSyncing = false" in detail_script
        assert "clearTimeout(this.metadataSyncTimer)" in detail_script

    async def test_series_detail_omits_catalog_state_banner_when_complete(
        self,
        authenticated_client,
        seeded_series_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/series/{seeded_series_detail_ui_data['series_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="series-detail-catalog-state-banner"' not in response.text

    async def test_missing_series_redirects_back_to_series_index(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series/999999", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/series"
