"""Route-contract tests for the rewritten /series page."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-series-ui-tests")

_EN_DASH = "\u2013"

_TEST_COVER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAEklEQVR42mMwqfiGFTGMSqAjAJZBnMFc9NzZAAAAAElFTkSuQmCC"
)


@pytest.fixture
async def seeded_series_ui_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed enough series data to exercise filters and pagination."""
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.library import LibraryRoot
    from pullbox.models.publisher import Publisher
    from pullbox.models.series import Series, SeriesStatus

    async with sec_db() as session:
        test_cover_url = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 180'%3E"
            "%3Crect width='120' height='180' fill='%23273343'/%3E"
            "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
            "fill='%23f4f4f5' font-family='sans-serif' font-size='16'%3EBatman%3C/text%3E%3C/svg%3E"
        )

        dc = Publisher(name="DC Comics")
        image = Publisher(name="Image Comics")
        root = LibraryRoot(name="UI Test Library", path="/tmp/series-ui", enabled=True)
        session.add_all([dc, image, root])
        await session.flush()

        seeded = [
            ("Batman", 2016, SeriesStatus.CONTINUING, True, dc),
            ("Batman Beyond", 2015, SeriesStatus.ENDED, False, dc),
            ("Saga", 2012, SeriesStatus.CONTINUING, True, image),
            ("Superman", 2018, SeriesStatus.CONTINUING, True, dc),
            ("Wonder Woman", 2019, SeriesStatus.ENDED, False, dc),
            ("Planetary", 1999, SeriesStatus.ENDED, False, image),
        ]

        for idx, (title, year, status, monitored, publisher) in enumerate(seeded, start=1):
            series_path = Path(f"/tmp/series-ui/{idx:02d}-{title.lower().replace(' ', '-')}")
            series_path.mkdir(parents=True, exist_ok=True)
            series = Series(
                title=title,
                sort_title=title,
                year_start=year,
                created_at=datetime(2024, 1, idx, tzinfo=UTC),
                status=status,
                monitored=monitored,
                cover_url=test_cover_url if title == "Batman" else None,
                issue_count=3,
                publisher_id=publisher.id,
                library_root_id=root.id,
                path=str(series_path),
            )
            session.add(series)
            await session.flush()
            if title == "Batman":
                (series_path / "cover.png").write_bytes(_TEST_COVER_PNG)
                series.cover_path = f"/api/v1/series/{series.id}/cover"
            if title == "Planetary":
                issues = [
                    Issue(
                        series_id=series.id,
                        issue_number=1.0,
                        title=f"{title} #1",
                        status=IssueStatus.OWNED,
                    ),
                    Issue(
                        series_id=series.id,
                        issue_number=2.0,
                        title=f"{title} #2",
                        status=IssueStatus.OWNED,
                    ),
                    Issue(
                        series_id=series.id,
                        issue_number=3.0,
                        title=f"{title} #3",
                        status=IssueStatus.OWNED,
                    ),
                ]
            else:
                issues = [
                    Issue(
                        series_id=series.id,
                        issue_number=1.0,
                        title=f"{title} #1",
                        status=IssueStatus.OWNED,
                    ),
                    Issue(
                        series_id=series.id,
                        issue_number=2.0,
                        title=f"{title} #2",
                        status=IssueStatus.WANTED,
                    ),
                    Issue(
                        series_id=series.id,
                        issue_number=3.0,
                        title=f"{title} #3",
                        status=IssueStatus.SKIPPED,
                    ),
                ]
            session.add_all(issues)

        await session.commit()


def _extract_pagination_urls(markup: str) -> list[str]:
    pattern = r'data-page-url="([^"]*page=\d[^"]*)"'
    return [html.unescape(url) for url in re.findall(pattern, markup)]


def _extract_series_titles(markup: str) -> list[str]:
    pattern = r'data-testid="series-item-link"[^>]*>\s*([^<]+)</a>'
    return [html.unescape(title.strip()) for title in re.findall(pattern, markup)]


@pytest.fixture
async def seeded_series_sort_ties(sec_db, seeded_series_ui_data) -> None:  # type: ignore[no-untyped-def]
    """Seed equal-key rows in reverse title order to expose unstable sorting."""
    from pullbox.models.library import LibraryRoot
    from pullbox.models.publisher import Publisher
    from pullbox.models.series import Series, SeriesStatus, SeriesType

    async with sec_db() as session:
        publisher = (
            await session.execute(select(Publisher).where(Publisher.name == "DC Comics"))
        ).scalar_one()
        root = (
            await session.execute(select(LibraryRoot).where(LibraryRoot.name == "UI Test Library"))
        ).scalar_one()
        tied_at = datetime(2035, 1, 1, tzinfo=UTC)
        for title in ("Zulu Sort Tie", "Alpha Sort Tie"):
            session.add(
                Series(
                    title=title,
                    sort_title=title.lower(),
                    year_start=2035,
                    created_at=tied_at,
                    status=SeriesStatus.ENDED,
                    monitored=False,
                    issue_count=0,
                    publisher_id=publisher.id,
                    library_root_id=root.id,
                    series_type=SeriesType.ANNUAL,
                )
            )
        await session.commit()


class SelectRecorder:
    """Capture SELECT statements emitted by the test app engine."""

    def __init__(self, async_engine) -> None:  # type: ignore[no-untyped-def]
        self._engine = async_engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self) -> SelectRecorder:
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


@pytest.mark.asyncio
class TestSeriesRouteContracts:
    """Verify the server-side /series rendering contract."""

    async def test_full_page_renders_static_toolbar_and_mounted_regions(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?status=continuing&per_page=2")

        assert response.status_code == 200
        assert 'data-testid="series-registry-header"' in response.text
        assert 'data-testid="series-registry-title"' in response.text
        assert "SERIES <span>REGISTRY</span>" in response.text
        assert 'data-testid="series-registry-subtitle"' in response.text
        assert 'data-testid="series-registry-gauges"' in response.text
        assert 'data-testid="series-registry-gauge-overall"' in response.text
        assert 'data-testid="series-registry-gauge-wanted"' in response.text
        assert 'data-testid="series-registry-gauge-active-downloads"' in response.text
        assert 'data-testid="series-registry-actions"' in response.text
        assert 'data-testid="series-pull-list-link"' not in response.text
        assert 'data-testid="series-toolbar"' in response.text
        assert 'data-testid="series-search-field"' in response.text
        assert 'data-testid="series-search-input"' in response.text
        assert 'data-testid="series-search-clear"' in response.text
        assert 'data-testid="series-search-history-panel"' in response.text
        assert 'data-search-field-contract="baseline-v2"' in response.text
        assert 'data-search-field-mode="remote"' in response.text
        assert 'data-search-field-debounce="250"' in response.text
        assert 'data-search-history-key="pullbox.searchHistory.series"' in response.text
        assert 'oninput="syncSearchFieldState(this); handleSearchFieldInput(this)"' in response.text
        assert 'x-model="value"' not in response.text
        assert 'x-show="value.length > 0"' not in response.text
        assert 'data-testid="series-status-select"' in response.text
        assert 'data-testid="series-monitored-select"' in response.text
        assert 'data-testid="series-sort-select"' in response.text
        assert 'data-testid="series-per-page-select"' in response.text
        assert response.text.count('data-dropdown-select-contract="v1"') >= 4
        assert 'data-testid="series-view-list"' in response.text
        assert 'data-testid="series-view-grid"' in response.text
        assert 'data-testid="series-view-toggle"' in response.text
        assert 'data-tip="List view"' in response.text
        assert 'data-tip="Grid view"' in response.text
        assert 'data-tip="Search missing"' in response.text
        assert 'data-tip="Open series"' in response.text
        assert 'data-tip-pos="left"' in response.text
        assert 'data-testid="series-view-compact"' not in response.text
        assert 'data-testid="series-select-mode-toggle"' in response.text
        assert "handleSelectionClick" in response.text
        assert "$event.shiftKey" in response.text
        assert "$event.metaKey || $event.ctrlKey" in response.text
        assert 'data-testid="series-select-toolbar"' in response.text
        assert 'data-testid="series-selection-controls-row"' in response.text
        assert 'data-testid="series-select-mode-done"' in response.text
        assert 'data-testid="series-bulk-actions"' in response.text
        assert 'data-testid="series-bulk-count"' in response.text
        assert 'data-testid="series-selection-inline"' in response.text
        assert "series-selection-inline-label" not in response.text
        assert 'data-testid="series-select-visible"' in response.text
        assert 'data-testid="series-select-all-results"' in response.text
        assert 'data-testid="series-deselect-all"' in response.text
        assert 'data-testid="series-bulk-monitor"' in response.text
        assert 'data-testid="series-bulk-unmonitor"' in response.text
        assert 'data-testid="series-bulk-delete"' in response.text
        assert "Select visible results or the full filtered set first" not in response.text
        assert ">Selection<" not in response.text
        assert "Bulk actions" not in response.text
        assert 'data-testid="series-delete-modal"' in response.text
        assert 'data-delete-modal-contract="series-v1"' in response.text
        assert 'data-testid="series-delete-submit"' in response.text
        assert 'data-testid="series-delete-warning-row"' in response.text
        assert 'data-testid="series-delete-summary"' in response.text
        assert 'data-testid="series-delete-options-header"' in response.text
        assert 'data-testid="series-delete-options-panel"' in response.text
        assert 'data-testid="series-mission-control-view"' in response.text
        assert 'data-testid="series-mission-control-table"' in response.text
        year_header_index = response.text.index('<th style="width: 64px">Year</th>')
        status_header_index = response.text.index('<th class="c" style="width: 92px">Status</th>')
        owned_header_index = response.text.index('<th class="c" style="width: 78px">Owned</th>')
        assert year_header_index < status_header_index < owned_header_index
        assert response.text.count('data-testid="series-lifecycle-status"') == 2
        assert 'data-series-status="continuing"' in response.text
        assert 'data-testid="series-collector-wall-view"' not in response.text
        assert 'data-testid="series-mission-control-footer"' not in response.text
        assert 'id="series-summary"' in response.text
        assert 'id="series-results-body"' in response.text
        assert 'data-testid="header-donations-button"' in response.text
        assert 'data-testid="header-theme-toggle"' in response.text
        assert 'data-testid="header-activity"' in response.text
        assert 'data-testid="header-activity-popover"' in response.text
        assert 'data-testid="header-activity-operation"' in response.text
        assert 'data-testid="header-activity-overall-progress"' in response.text
        assert 'data-testid="header-activity-item-progress"' in response.text
        assert 'data-testid="header-activity-view-details"' in response.text
        assert 'aria-label="Background activity"' in response.text
        assert 'data-testid="live-updates-toggle"' not in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert 'data-testid="page-dock-pagination"' in response.text
        assert 'data-testid="app-footer"' not in response.text
        assert (
            '<h1 class="text-lg font-display font-extrabold text-pb-text tracking-tight">'
            not in response.text
        )
        assert "transition:true" not in response.text

    async def test_list_view_renders_ended_and_continuing_status_pills(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?per_page=20")

        assert response.status_code == 200
        assert response.text.count('data-testid="series-lifecycle-status"') == 6
        assert 'data-series-status="continuing"' in response.text
        assert 'data-series-status="ended"' in response.text
        assert '<span class="pill pill-success">\n  Continuing\n</span>' in response.text
        assert '<span class="pill pill-neutral">\n  Ended\n</span>' in response.text

    async def test_htmx_request_returns_oob_bundle_only(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/series?status=continuing&per_page=2",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'id="series-summary" hx-swap-oob="innerHTML"' in response.text
        assert 'id="page-footer-dock" hx-swap-oob="innerHTML"' in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert 'data-testid="series-result-row"' in response.text
        assert 'data-testid="series-row-checkbox"' in response.text
        assert '@change="' not in response.text
        assert ' :checked="' not in response.text
        assert ' :class="' not in response.text
        assert 'data-testid="series-select-toolbar"' not in response.text
        assert 'data-testid="series-delete-modal"' not in response.text
        assert 'id="content"' not in response.text
        assert "<html" not in response.text.lower()
        assert "transition:true" not in response.text

    async def test_series_page_counts_issues_without_eager_loading_issue_rows(
        self,
        authenticated_client,
        seeded_series_ui_data,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        engine = sec_db.kw["bind"]
        with SelectRecorder(engine) as recorder:
            response = await authenticated_client.get("/series?status=continuing&per_page=2")

        assert response.status_code == 200
        assert 'data-testid="series-result-row"' in response.text
        assert all("issues_1" not in statement for statement in recorder.statements)

    async def test_title_sort_aggregates_issue_counts_for_visible_page_only(
        self,
        authenticated_client,
        seeded_series_ui_data,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        """Default sorting must not group every issue solely to render one page."""
        engine = sec_db.kw["bind"]
        with SelectRecorder(engine) as recorder:
            response = await authenticated_client.get("/series?sort=title&per_page=2")

        assert response.status_code == 200
        grouped_issue_queries = [
            statement
            for statement in recorder.statements
            if "GROUP BY issues.series_id" in statement
        ]
        assert grouped_issue_queries
        assert any("WHERE issues.series_id IN" in statement for statement in grouped_issue_queries)

    async def test_series_cards_render_status_aware_year_ranges(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?per_page=20")

        assert response.status_code == 200
        assert f"2016{_EN_DASH}present" in response.text
        assert f"2015{_EN_DASH}2015" not in response.text
        assert f"2019{_EN_DASH}2019" not in response.text

    async def test_pagination_mount_hides_when_results_fit_on_one_page(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?per_page=25")

        assert response.status_code == 200
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert "page-dock-inner page-dock-inner-status-only" in response.text
        assert 'data-testid="page-dock-pagination"' not in response.text
        assert 'data-testid="series-pagination-next"' not in response.text
        assert 'data-testid="series-pagination-prev"' not in response.text

    async def test_per_page_zero_falls_back_to_bounded_page_size(
        self,
        authenticated_client,
        seeded_series_ui_data,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.series import Series, SeriesStatus

        async with sec_db() as session:
            for idx in range(30):
                session.add(
                    Series(
                        title=f"Bounded Series {idx:02d}",
                        sort_title=f"Bounded Series {idx:02d}",
                        year_start=2026,
                        created_at=datetime(2024, 7, 1, tzinfo=UTC),
                        status=SeriesStatus.CONTINUING,
                        monitored=True,
                    )
                )
            await session.commit()

        response = await authenticated_client.get("/series?per_page=0")

        assert response.status_code == 200
        assert response.text.count('data-testid="series-result-row"') == 25
        assert "All results" not in response.text

    async def test_legacy_compact_cookie_falls_back_to_list_markup(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        authenticated_client.cookies.set("series_view", "compact")
        response = await authenticated_client.get("/series?status=continuing&per_page=2")

        assert response.status_code == 200
        assert 'data-series-active-view="list"' in response.text
        assert 'data-testid="series-view-compact"' not in response.text
        assert 'data-testid="series-mission-control-view"' in response.text
        assert 'data-testid="series-collector-wall-view"' not in response.text
        assert 'data-testid="series-compact-card"' not in response.text

    async def test_cookie_backed_grid_view_renders_only_grid_markup(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get("/series?status=continuing&per_page=2")

        assert response.status_code == 200
        assert 'data-series-active-view="grid"' in response.text
        assert 'data-testid="series-view-grid"' in response.text
        assert 'data-testid="series-collector-wall-view"' in response.text
        assert 'data-testid="series-mission-control-view"' not in response.text
        assert 'data-testid="series-grid-card"' in response.text
        assert 'class="series-wall-card-meta"' not in response.text
        assert 'data-testid="series-grid-hover-meta"' in response.text
        assert 'data-testid="series-grid-hover-publisher"' in response.text
        assert 'data-testid="series-grid-hover-years"' in response.text
        assert 'data-testid="series-grid-hover-type"' not in response.text
        assert 'data-testid="series-grid-hover-owned"' in response.text
        assert 'data-testid="series-monitored-indicator"' in response.text
        assert 'aria-label="Monitored"' in response.text
        assert 'data-testid="series-compact-view"' not in response.text
        assert "Visual shelf" not in response.text
        assert "Cover-first browsing" not in response.text

    async def test_list_and_grid_add_private_reading_aggregate_without_replacing_acquisition(
        self,
        authenticated_client,
        sec_db,
        sec_user,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.issue import Issue
        from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
        from pullbox.models.reader import IssueReaderState
        from pullbox.models.series import Series

        now = datetime.now(UTC)
        async with sec_db() as session:
            series = (
                await session.execute(select(Series).where(Series.title == "Batman"))
            ).scalar_one()
            issue = (
                await session.execute(
                    select(Issue).where(
                        Issue.series_id == series.id,
                        Issue.issue_number == 1.0,
                    )
                )
            ).scalar_one()
            root = (await session.execute(select(LibraryRoot))).scalars().first()
            assert root is not None
            session.add_all(
                [
                    LibraryFile(
                        issue_id=issue.id,
                        library_root_id=root.id,
                        file_path="/tmp/series-ui/01-batman/Batman 001.cbz",
                        file_name="Batman 001.cbz",
                        file_size=1024,
                        file_format=FileFormat.CBZ,
                        file_modified_at=now,
                        match_confidence=MatchConfidence.HIGH,
                    ),
                    IssueReaderState(
                        user_id=sec_user.id,
                        issue_id=issue.id,
                        completed_at=now,
                        completion_updated_at=now,
                    ),
                ]
            )
            await session.commit()

        list_response = await authenticated_client.get("/series?q=Batman&per_page=20")
        authenticated_client.cookies.set("series_view", "grid")
        grid_response = await authenticated_client.get("/series?q=Batman&per_page=20")

        assert list_response.status_code == 200
        assert 'data-testid="series-list-reading"' in list_response.text
        assert "Read 1/1" in list_response.text
        assert 'style="width: 33%"' in list_response.text
        assert grid_response.status_code == 200
        assert 'data-testid="series-grid-hover-reading"' in grid_response.text
        assert "Read 1/1" in grid_response.text
        assert "stroke-dashoffset=" in grid_response.text

    async def test_series_registry_adds_exactly_one_visible_reading_aggregate_query(
        self,
        authenticated_client,
        sec_db,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        engine = sec_db.kw["bind"]
        with SelectRecorder(engine) as recorder:
            response = await authenticated_client.get("/series?sort=title&per_page=2")

        assert response.status_code == 200
        reader_queries = [
            statement for statement in recorder.statements if "issue_reader_states" in statement
        ]
        assert len(reader_queries) == 1
        assert "GROUP BY issues.series_id" in reader_queries[0]
        assert "WHERE issues.series_id IN" in reader_queries[0]

    async def test_series_list_surfaces_catalog_sync_state(
        self,
        authenticated_client,
        sec_db,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.series import IssueCatalogState, Series

        async with sec_db() as session:
            series = (
                await session.execute(select(Series).where(Series.title == "Batman"))
            ).scalar_one()
            series.issue_catalog_state = IssueCatalogState.HYDRATING
            await session.commit()

        response = await authenticated_client.get("/series?q=Batman")

        assert response.status_code == 200
        assert 'data-testid="series-catalog-state-badge"' in response.text
        assert 'data-catalog-refresh-active="true"' in response.text
        assert 'hx-trigger="every 10s"' in response.text
        assert 'hx-swap="morph:outerHTML"' in response.text
        assert 'hx-push-url="false"' in response.text
        assert 'hx-sync="#series-results-body:replace"' in response.text
        assert "Metadata syncing" in response.text

        fragment_response = await authenticated_client.get(
            "/series?q=Batman",
            headers={"HX-Request": "true"},
        )

        assert fragment_response.status_code == 200
        assert 'data-catalog-refresh-active="true"' in fragment_response.text
        assert 'hx-swap="morph:outerHTML"' in fragment_response.text

    async def test_series_grid_surfaces_catalog_sync_state(
        self,
        authenticated_client,
        sec_db,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.series import IssueCatalogState, Series

        async with sec_db() as session:
            series = (
                await session.execute(select(Series).where(Series.title == "Batman"))
            ).scalar_one()
            series.issue_catalog_state = IssueCatalogState.FAILED
            await session.commit()

        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get("/series?q=Batman")

        assert response.status_code == 200
        assert 'data-testid="series-catalog-state-badge"' in response.text
        assert 'data-catalog-refresh-active="true"' not in response.text
        assert "Metadata needs retry" in response.text

    async def test_series_results_do_not_poll_when_catalogs_are_complete(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?q=Batman")

        assert response.status_code == 200
        assert 'data-catalog-refresh-active="true"' not in response.text

    async def test_grid_view_hides_cover_monitor_indicator_for_paused_series(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get(
            "/series?status=ended&q=Batman%20Beyond&per_page=5"
        )

        assert response.status_code == 200
        assert "Batman Beyond" in response.text
        assert 'data-testid="series-monitored-indicator"' not in response.text

    async def test_grid_view_hover_shows_full_non_standard_series_type(
        self,
        authenticated_client,
        sec_db,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.library import LibraryRoot
        from pullbox.models.publisher import Publisher
        from pullbox.models.series import Series, SeriesStatus, SeriesType

        async with sec_db() as session:
            publisher = (
                await session.execute(select(Publisher).where(Publisher.name == "DC Comics"))
            ).scalar_one()
            library_root = (await session.execute(select(LibraryRoot))).scalar_one()
            session.add(
                Series(
                    title="Grid Type Feature",
                    sort_title="Grid Type Feature",
                    year_start=2026,
                    status=SeriesStatus.ENDED,
                    monitored=False,
                    issue_count=1,
                    publisher_id=publisher.id,
                    library_root_id=library_root.id,
                    series_type=SeriesType.GRAPHIC_NOVEL,
                )
            )
            await session.commit()

        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get("/series?q=Grid%20Type%20Feature&per_page=5")

        assert response.status_code == 200
        assert 'data-testid="series-grid-hover-type"' in response.text
        assert "Graphic Novel" in response.text

    async def test_series_views_render_versioned_cover_urls(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get("/series?per_page=20")

        assert response.status_code == 200
        assert "/api/v1/series/" in response.text
        assert "/cover?v=" in response.text

    async def test_grid_search_action_escapes_quoted_titles_for_alpine(
        self,
        authenticated_client,
        sec_db,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.issue import Issue, IssueStatus
        from pullbox.models.library import LibraryRoot
        from pullbox.models.publisher import Publisher
        from pullbox.models.series import Series, SeriesStatus

        quoted_title = '[manual test] "alpha signal"'

        async with sec_db() as session:
            publisher = (
                await session.execute(select(Publisher).where(Publisher.name == "DC Comics"))
            ).scalar_one()
            library_root = (await session.execute(select(LibraryRoot))).scalar_one()

            series = Series(
                title=quoted_title,
                sort_title=quoted_title,
                year_start=2026,
                created_at=datetime(2024, 6, 1, tzinfo=UTC),
                status=SeriesStatus.CONTINUING,
                monitored=True,
                issue_count=2,
                publisher_id=publisher.id,
                library_root_id=library_root.id,
            )
            session.add(series)
            await session.flush()

            session.add_all(
                [
                    Issue(
                        series_id=series.id,
                        issue_number=1.0,
                        title=f"{quoted_title} #1",
                        status=IssueStatus.OWNED,
                    ),
                    Issue(
                        series_id=series.id,
                        issue_number=2.0,
                        title=f"{quoted_title} #2",
                        status=IssueStatus.WANTED,
                    ),
                ]
            )
            await session.commit()

        authenticated_client.cookies.set("series_view", "grid")
        response = await authenticated_client.get("/series?per_page=25")

        assert response.status_code == 200
        assert "alpha signal" in response.text

        match = re.search(
            r'@click\.prevent="runRowSearch\(\{ id: '
            + str(series.id)
            + r', title: ([^,]+), disabled: false \}\)"',
            response.text,
        )
        assert match is not None
        assert html.unescape(match.group(1)) == json.dumps(quoted_title)

    async def test_paused_complete_series_keeps_green_acquisition_tone(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series?per_page=25")

        assert response.status_code == 200
        assert re.search(
            (
                r"Planetary.*?series-led series-led-off.*?"
                r"series-mission-control-bar-fill "
                r"series-mission-control-bar-fill-green.*?"
                r"series-mission-control-bar-pct-green\">100%</span>"
            ),
            response.text,
            re.S,
        )

    async def test_add_series_page_renders_standardized_shell(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series/add")

        assert response.status_code == 200
        assert 'data-testid="add-series-page"' in response.text
        assert 'data-testid="add-series-header"' in response.text
        assert 'data-testid="add-series-title"' in response.text
        assert "ADD <span>SERIES</span>" in response.text
        assert 'data-testid="add-series-subtitle"' in response.text
        assert 'data-testid="add-series-header-metrics"' in response.text
        assert 'data-testid="add-series-gauges"' in response.text
        assert 'data-testid="add-series-gauge-results"' in response.text
        assert 'data-testid="add-series-gauge-in-library"' in response.text
        assert 'data-testid="add-series-gauge-ready"' not in response.text
        assert 'data-testid="add-series-search-form"' in response.text
        assert 'data-testid="add-series-search-field"' in response.text
        assert 'data-testid="add-series-search-input"' in response.text
        assert 'data-testid="add-series-search-clear"' in response.text
        assert 'data-testid="add-series-search-history-panel"' in response.text
        assert 'data-testid="add-series-search-indicator"' not in response.text
        assert 'data-testid="add-series-results-loading"' in response.text
        assert 'hx-sync="#add-series-search-form:replace"' in response.text
        assert 'hx-indicator="#add-series-results-loading"' in response.text
        assert 'data-search-field-contract="baseline-v2"' in response.text
        assert 'data-search-field-mode="remote"' in response.text
        assert 'data-search-field-debounce="250"' in response.text
        assert 'data-search-history-key="pullbox.searchHistory.addSeries"' in response.text
        assert (
            'class="search-field control-size-sm series-registry-search '
            'add-series-search-field"' in response.text
        )
        assert 'data-testid="add-series-sort-select"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'data-testid="add-series-results"' in response.text
        assert 'data-testid="add-series-empty-state"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="add-series-footer-dock"' in response.text
        assert 'data-testid="add-series-root-display"' in response.text
        assert "readonly" in response.text
        assert 'data-testid="add-series-root-select"' not in response.text
        assert 'data-testid="add-series-search-on-add-control"' not in response.text
        assert '<select x-model="libraryRootId"' not in response.text

    async def test_add_series_search_results_return_bundle_with_oob_header_and_footer(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        mock_result = SeriesSearchResult(
            provider_id="12345",
            title="Batman",
            year_start=2016,
            publisher="DC Comics",
            issue_count=85,
            status=None,
            cover_url="https://example.test/batman.jpg",
            description="A test result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ),
        ):
            response = await authenticated_client.get("/htmx/series/search?q=Batman&sort=relevance")

        assert response.status_code == 200
        assert 'id="add-series-header-metrics"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'data-testid="add-series-footer-dock"' in response.text
        assert 'data-testid="add-series-results"' in response.text
        assert 'data-testid="add-series-result-card"' in response.text
        assert 'onclick="selectResult(12345' in response.text
        assert "Add" in response.text
        assert "Add series" not in response.text

    async def test_add_series_live_preview_uses_lightweight_page_search(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        mock_result = SeriesSearchResult(
            provider_id="170671",
            title="The Punisher",
            year_start=2026,
            publisher="Marvel",
            issue_count=4,
            status=None,
            cover_url="https://example.test/punisher.jpg",
            description="A quick preview result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_page",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ) as page_search,
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
            ) as global_search,
        ):
            response = await authenticated_client.get(
                "/series/add?q=The%20Punisher&search_mode=preview",
                headers={"HX-Request": "true"},
            )

        assert response.status_code == 200
        assert "The Punisher (2026)" in response.text
        assert 'data-testid="add-series-preview-notice"' in response.text
        assert "Quick preview" in response.text
        assert "Quick Matches" in response.text
        assert "Preview" in response.text
        assert "Search all ComicVine results" in response.text
        assert 'data-testid="add-series-full-search-button"' in response.text
        assert 'hx-get="/series/add?q=The+Punisher&amp;sort=relevance"' in response.text
        assert "search_mode=preview" not in response.text
        page_search.assert_awaited_once_with("The Punisher", None, limit=20)
        global_search.assert_not_awaited()

    async def test_add_series_full_search_does_not_show_preview_notice(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        mock_result = SeriesSearchResult(
            provider_id="170671",
            title="The Punisher",
            year_start=2026,
            publisher="Marvel",
            issue_count=4,
            status=None,
            cover_url="https://example.test/punisher.jpg",
            description="A full search result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_page",
                new_callable=AsyncMock,
            ) as page_search,
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ) as global_search,
        ):
            response = await authenticated_client.get(
                "/series/add?q=The%20Punisher",
                headers={"HX-Request": "true"},
            )

        assert response.status_code == 200
        assert "The Punisher (2026)" in response.text
        assert 'data-testid="add-series-preview-notice"' not in response.text
        assert 'data-testid="add-series-full-search-button"' not in response.text
        assert "Quick Matches" not in response.text
        assert "Full search" in response.text
        page_search.assert_not_awaited()
        from pullbox.ui.series_routes import COMICVINE_SERIES_SEARCH_LIMIT

        global_search.assert_awaited_once_with(
            "The Punisher",
            max_results=COMICVINE_SERIES_SEARCH_LIMIT,
            batch_size=100,
            suppress_errors=True,
        )

    async def test_add_series_full_search_reuses_persistent_cache_between_requests(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        mock_result = SeriesSearchResult(
            provider_id="180001",
            title="Cache Candidate",
            year_start=2026,
            publisher="Test Comics",
            issue_count=1,
            status=None,
            cover_url=None,
            description="A cached full search result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ) as global_search,
        ):
            first = await authenticated_client.get(
                "/series/add?q=Cache%20Candidate",
                headers={"HX-Request": "true"},
            )
            second = await authenticated_client.get(
                "/series/add?q=cache%20candidate",
                headers={"HX-Request": "true"},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert "Cache Candidate (2026)" in first.text
        assert "Cache Candidate (2026)" in second.text
        global_search.assert_awaited_once()

    async def test_add_series_live_preview_ignores_broad_partial_queries(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
            ) as api_key,
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_page",
                new_callable=AsyncMock,
            ) as page_search,
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
            ) as global_search,
        ):
            response = await authenticated_client.get(
                "/series/add?q=th&search_mode=preview",
                headers={"HX-Request": "true"},
            )

        assert response.status_code == 200
        assert 'data-testid="add-series-empty-state"' in response.text
        api_key.assert_not_awaited()
        page_search.assert_not_awaited()
        global_search.assert_not_awaited()

    async def test_add_series_search_results_use_media_folder_naming_for_modal_preview(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.models.config import SystemConfig
        from pullbox.providers.base import SeriesSearchResult

        async with sec_db() as session:
            session.add_all(
                [
                    SystemConfig(
                        key="series_folder_template",
                        value="{Publisher} - {Series} [{Year}]",
                    ),
                    SystemConfig(key="replace_illegal_characters", value="true"),
                    SystemConfig(key="colon_replacement", value="dash"),
                ]
            )
            await session.commit()

        mock_result = SeriesSearchResult(
            provider_id="12345",
            title="Ultimate Spider-Man",
            year_start=2024,
            publisher="Marvel",
            issue_count=14,
            status=None,
            cover_url="https://example.test/ultimate-spider-man.jpg",
            description="A test result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ),
        ):
            response = await authenticated_client.get("/htmx/series/search?q=Ultimate")

        assert response.status_code == 200
        assert 'data-series-folder-preview="Marvel - Ultimate Spider-Man [2024]"' in response.text

    async def test_add_series_search_results_sort_by_requested_order(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        older = SeriesSearchResult(
            provider_id="12345",
            title="Batman",
            year_start=2000,
            publisher="DC Comics",
            issue_count=160,
            status=None,
            cover_url="https://example.test/batman-2000.jpg",
            description="Older run",
        )
        newer = SeriesSearchResult(
            provider_id="67890",
            title="Batman",
            year_start=2024,
            publisher="DC Comics",
            issue_count=14,
            status=None,
            cover_url="https://example.test/batman-2024.jpg",
            description="Newer run",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([older, newer], 2),
            ),
        ):
            oldest_first = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=year_start"
            )
            newest_first = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=-year_start"
            )

        assert oldest_first.status_code == 200
        assert newest_first.status_code == 200
        assert oldest_first.text.index("Batman (2000)") < oldest_first.text.index("Batman (2024)")
        assert newest_first.text.index("Batman (2024)") < newest_first.text.index("Batman (2000)")

    async def test_add_series_search_uses_trailing_year_as_start_year_hint(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        panini_result = SeriesSearchResult(
            provider_id="170049",
            title="X-Men",
            year_start=2025,
            publisher="Panini Comics",
            issue_count=4,
            status=None,
            cover_url="https://example.test/x-men-2025.jpg",
            description="Wrong regional volume",
        )
        correct_result = SeriesSearchResult(
            provider_id="158814",
            title="X-Men",
            year_start=2024,
            publisher="Marvel",
            issue_count=29,
            status=None,
            cover_url="https://example.test/x-men-2024.jpg",
            description="Correct Marvel volume",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([panini_result, correct_result], 2),
            ) as search_mock,
        ):
            response = await authenticated_client.get("/htmx/series/search?q=X-Men%202024")

        assert response.status_code == 200
        search_mock.assert_awaited_once_with(
            "X-Men",
            max_results=1000,
            batch_size=100,
            suppress_errors=True,
        )
        assert response.text.index("X-Men (2024)") < response.text.index("X-Men (2025)")

    async def test_add_series_newest_year_sort_is_global_across_fetched_results(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        results = [
            SeriesSearchResult(
                provider_id=str(1000 + index),
                title=f"The Punisher Variant {index}",
                year_start=1990 + index,
                publisher="Marvel",
                issue_count=4,
                status=None,
                cover_url="https://example.test/punisher.jpg",
                description="Older relevance result",
            )
            for index in range(20)
        ]
        results.append(
            SeriesSearchResult(
                provider_id="168720",
                title="Daredevil/Punisher: The Devil's Trigger",
                year_start=2026,
                publisher="Marvel",
                issue_count=5,
                status=None,
                cover_url="https://example.test/daredevil-punisher.jpg",
                description="Same-year crossover result",
            )
        )
        results.append(
            SeriesSearchResult(
                provider_id="170671",
                title="The Punisher",
                year_start=2026,
                publisher="Marvel",
                issue_count=4,
                status=None,
                cover_url="https://example.test/punisher-2026.jpg",
                description="Result that ComicVine relevance placed after page one",
            )
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=(results, len(results)),
            ),
        ):
            response = await authenticated_client.get(
                "/htmx/series/search?q=Punisher&sort=-year_start"
            )

        assert response.status_code == 200
        assert "The Punisher (2026)" in response.text
        assert response.text.index("The Punisher (2026)") < response.text.index(
            "Daredevil/Punisher: The Devil&#39;s Trigger (2026)"
        )
        assert response.text.index("The Punisher (2026)") < response.text.index(
            "The Punisher Variant 19 (2009)"
        )

    async def test_add_series_best_match_prioritizes_exact_title_over_provider_order(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        results = [
            SeriesSearchResult(
                provider_id="168720",
                title="Daredevil/Punisher: The Devil's Trigger",
                year_start=2026,
                publisher="Marvel",
                issue_count=5,
                status=None,
                cover_url="https://example.test/daredevil-punisher.jpg",
                description="Provider-first weak match",
            ),
            SeriesSearchResult(
                provider_id="170671",
                title="The Punisher",
                year_start=2026,
                publisher="Marvel",
                issue_count=4,
                status=None,
                cover_url="https://example.test/punisher-2026.jpg",
                description="Exact title match",
            ),
            SeriesSearchResult(
                provider_id="13057",
                title="Punisher War Journal",
                year_start=2006,
                publisher="Marvel",
                issue_count=26,
                status=None,
                cover_url="https://example.test/punisher-war-journal.jpg",
                description="Starts-with title match",
            ),
        ]

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=(results, len(results)),
            ),
        ):
            response = await authenticated_client.get("/htmx/series/search?q=Punisher")

        assert response.status_code == 200
        assert response.text.index("The Punisher (2026)") < response.text.index(
            "Punisher War Journal (2006)"
        )
        assert response.text.index("Punisher War Journal (2006)") < response.text.index(
            "Daredevil/Punisher: The Devil&#39;s Trigger (2026)"
        )

    async def test_add_series_search_results_include_footer_pagination_when_multiple_pages(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        paged_results = [
            SeriesSearchResult(
                provider_id=str(1000 + index),
                title=f"Batman Variant {index}",
                year_start=2010 + index,
                publisher="DC Comics",
                issue_count=12 + index,
                status=None,
                cover_url="https://example.test/batman.jpg",
                description="A paged result",
            )
            for index in range(45)
        ]

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=(paged_results, 45),
            ) as mock_search,
        ):
            response = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=title&page=2"
            )

        assert response.status_code == 200
        mock_search.assert_awaited_once_with(
            "Batman",
            max_results=1000,
            batch_size=100,
            suppress_errors=True,
        )
        assert 'data-testid="add-series-footer-dock"' in response.text
        assert 'data-testid="page-dock-pagination"' in response.text
        assert 'data-testid="series-pagination-prev"' in response.text
        assert 'data-testid="series-pagination-next"' in response.text
        assert 'hx-sync="#add-series-search-form:replace"' in response.text
        assert 'hx-indicator="#add-series-results-loading"' in response.text
        assert 'hx-swap="outerHTML show:#content:top"' in response.text
        assert 'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=1"' in response.text
        assert 'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=3"' in response.text

    async def test_add_series_search_results_shift_visible_page_clusters_with_current_page(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        paged_results = [
            SeriesSearchResult(
                provider_id=str(2000 + index),
                title=f"Batman Deep Page {index}",
                year_start=1990 + index,
                publisher="DC Comics",
                issue_count=24 + index,
                status=None,
                cover_url="https://example.test/batman.jpg",
                description="A fixed-width pager result",
            )
            for index in range(220)
        ]

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=(paged_results, 220),
            ),
        ):
            middle_response = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=title&page=6"
            )
            later_response = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=title&page=8"
            )

        assert middle_response.status_code == 200
        assert later_response.status_code == 200

        assert middle_response.text.count("&hellip;") == 1
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=5"' in middle_response.text
        )
        assert 'id="pagination-page-6"' in middle_response.text
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=10"'
            in middle_response.text
        )
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=11"'
            in middle_response.text
        )
        assert 'data-testid="series-pagination-page-1"' not in middle_response.text
        assert 'data-testid="series-pagination-page-2"' not in middle_response.text

        assert later_response.text.count("&hellip;") == 1
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=1"' in later_response.text
        )
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=2"' in later_response.text
        )
        assert 'id="pagination-page-8"' in later_response.text
        assert (
            'data-page-url="/series/add?q=Batman&amp;sort=title&amp;page=9"' in later_response.text
        )
        assert 'data-testid="series-pagination-page-10"' not in later_response.text
        assert 'data-testid="series-pagination-page-11"' not in later_response.text

    async def test_add_series_formats_only_visible_page_after_global_sort(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        paged_results = [
            SeriesSearchResult(
                provider_id=str(3000 + index),
                title=f"Batman Variant {index:03d}",
                year_start=2000 + index,
                publisher="DC Comics",
                issue_count=12 + index,
                status=None,
                cover_url="https://example.test/batman.jpg",
                description="A paged result",
            )
            for index in range(45)
        ]

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=(paged_results, 45),
            ),
            patch(
                "pullbox.ui.comicvine_series_search.format_series_folder",
                return_value="Folder Preview",
            ) as folder_preview,
        ):
            response = await authenticated_client.get(
                "/htmx/series/search?q=Batman&sort=title&page=2"
            )

        assert response.status_code == 200
        assert "Batman Variant 020" in response.text
        assert "Batman Variant 039" in response.text
        assert "Batman Variant 040" not in response.text
        assert folder_preview.call_count == 20

    async def test_add_series_search_results_use_shared_add_button_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.providers.base import SeriesSearchResult

        mock_result = SeriesSearchResult(
            provider_id="12345",
            title="Batman",
            year_start=2016,
            publisher="DC Comics",
            issue_count=85,
            status=None,
            cover_url="https://example.test/batman.jpg",
            description="A test result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ),
        ):
            response = await authenticated_client.get("/htmx/series/search?q=Batman")

        assert response.status_code == 200
        assert 'data-add-series-trigger="true"' in response.text
        assert "Add" in response.text

    async def test_add_series_search_results_link_existing_library_matches(
        self,
        authenticated_client,
        seeded_series_ui_data,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from sqlalchemy import select

        from pullbox.models.series import Series
        from pullbox.providers.base import SeriesSearchResult

        async with sec_db() as session:
            series = await session.scalar(select(Series).where(Series.title == "Batman"))
            assert series is not None
            series.comicvine_id = 12345
            await session.commit()
            existing_series_id = series.id

        mock_result = SeriesSearchResult(
            provider_id="12345",
            title="Batman",
            year_start=2016,
            publisher="DC Comics",
            issue_count=85,
            status=None,
            cover_url="https://example.test/batman.jpg",
            description="A test result",
        )

        with (
            patch(
                "pullbox.core.comicvine_key.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="fake-key",
            ),
            patch(
                "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                new_callable=AsyncMock,
                return_value=([mock_result], 1),
            ),
        ):
            response = await authenticated_client.get("/htmx/series/search?q=Batman")

        assert response.status_code == 200
        assert f'href="/series/{existing_series_id}"' in response.text
        assert 'data-testid="add-series-existing-cover-link"' in response.text
        assert 'data-testid="add-series-existing-title-link"' in response.text
        assert 'data-add-series-trigger="true"' not in response.text
        assert "In Library" in response.text

    async def test_series_cover_endpoint_serves_local_cached_cover(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/api/v1/series/1/cover")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"

    async def test_series_selection_ids_returns_all_matching_series(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series/selection-ids?q=Batman")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert len(payload["ids"]) == 2

    async def test_pagination_urls_preserve_active_query_params(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/series?status=continuing&monitored=true&sort=-title&per_page=1"
        )

        assert response.status_code == 200
        urls = _extract_pagination_urls(response.text)
        assert urls

        parsed = [parse_qs(urlparse(url).query) for url in urls]
        assert any(query.get("page") == ["2"] for query in parsed)
        assert all(query.get("status") == ["continuing"] for query in parsed)
        assert all(query.get("monitored") == ["true"] for query in parsed)
        assert all(query.get("sort") == ["-title"] for query in parsed)
        assert all(query.get("per_page") == ["1"] for query in parsed)

    async def test_series_sort_dropdown_includes_acquisition_options(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        descending = await authenticated_client.get("/series?sort=-acquisition&per_page=25")
        ascending = await authenticated_client.get("/series?sort=acquisition&per_page=25")

        assert descending.status_code == 200
        assert 'data-dropdown-value="-acquisition"' in descending.text
        assert "Acquisition (Most to Least)" in descending.text
        assert ascending.status_code == 200
        assert 'data-dropdown-value="acquisition"' in ascending.text
        assert "Acquisition (Least to Most)" in ascending.text

    async def test_acquisition_sort_orders_by_completion_percentage(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        ascending = await authenticated_client.get("/series?sort=acquisition&per_page=100")
        descending = await authenticated_client.get("/series?sort=-acquisition&per_page=100")

        assert ascending.status_code == 200
        assert descending.status_code == 200
        assert _extract_series_titles(ascending.text)[0] == "Batman"
        assert _extract_series_titles(descending.text)[0] == "Planetary"

    @pytest.mark.parametrize(
        "sort",
        [
            "date_added",
            "-date_added",
            "year",
            "-year",
            "publisher",
            "-publisher",
            "issues",
            "-issues",
            "acquisition",
            "-acquisition",
            "series_type",
            "-series_type",
            "status",
        ],
    )
    async def test_grouped_sorts_use_title_tiebreakers(
        self,
        authenticated_client,
        seeded_series_sort_ties,
        sort: str,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(f"/series?sort={sort}&per_page=100")

        assert response.status_code == 200
        titles = _extract_series_titles(response.text)
        assert titles.index("Alpha Sort Tie") < titles.index("Zulu Sort Tie")

    async def test_year_sort_is_deterministic_across_page_boundaries(
        self,
        authenticated_client,
        seeded_series_sort_ties,
    ) -> None:  # type: ignore[no-untyped-def]
        first_page = await authenticated_client.get("/series?sort=-year&per_page=1&page=1")
        second_page = await authenticated_client.get("/series?sort=-year&per_page=1&page=2")

        assert first_page.status_code == 200
        assert second_page.status_code == 200
        assert _extract_series_titles(first_page.text) == ["Alpha Sort Tie"]
        assert _extract_series_titles(second_page.text) == ["Zulu Sort Tie"]

    async def test_empty_state_renders_inside_mounted_results_contract(
        self,
        authenticated_client,
        seeded_series_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/series?q=TotallyMissingSeries",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'id="series-summary" hx-swap-oob="innerHTML"' in response.text
        assert 'data-testid="series-empty-state"' in response.text
        assert "No series matching" in response.text
