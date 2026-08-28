"""Focused E2E coverage for the series detail rewrite."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.pages.series_detail import SeriesDetailPage
from tests.e2e.pages.series_list import SeriesListPage

pytestmark = pytest.mark.e2e


class TestSeriesDetailPage:
    """Behavior-first E2E coverage for /series/{id}."""

    def test_monitored_indicator_tooltip_renders_on_hover(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        target = authed_page.locator(
            "[data-testid='series-detail-status-row'] .series-domain-led"
        ).first
        target.hover()

        assert target.get_attribute("data-tip") == "Monitored"

    def test_initial_load_renders_stable_series_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        header_box = authed_page.locator("[data-testid='app-header']").bounding_box()
        hero_box = series.hero.bounding_box()

        assert header_box is not None
        assert hero_box is not None
        assert series.page_shell.is_visible()
        assert series.back_link.is_visible()
        assert series.hero.is_visible()
        assert series.hero_summary_panel.is_visible()
        title_link = authed_page.locator("[data-testid='series-detail-title-link']").first
        assert title_link.is_visible()
        assert title_link.get_attribute("href") == (
            "https://comicvine.gamespot.com/batman/4050-12345/"
        )
        assert title_link.get_attribute("target") == "_blank"
        assert series.hero_actions_panel.is_visible()
        assert authed_page.locator("[data-testid='series-detail-gauge-row']").count() == 0
        assert authed_page.locator("[data-testid='series-detail-acquisition-bar']").count() == 0
        assert series.monitor_control.is_visible()
        assert series.monitor_label.is_visible()
        assert series.monitor_label.text_content() == "Monitored"
        assert series.monitor_toggle.is_visible()
        assert series.monitor_toggle.is_checked()
        assert series.issues_section.is_visible()
        assert series.footer.is_visible()
        assert authed_page.locator("[data-testid='series-detail-telemetry-strip']").count() == 0
        assert hero_box["y"] >= header_box["y"] + header_box["height"] + 12

    def test_private_reading_state_is_distinct_from_acquisition_progress(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        summary = authed_page.locator("[data-testid='series-reading-summary']").first
        reading = authed_page.locator("#issue-1 [data-testid='series-issue-reading']")
        acquisition = authed_page.locator(".series-domain-issues-progress-label").first

        expect(summary).to_have_text("Read 0 of 1 readable")
        expect(reading).to_contain_text("Page 2/3")
        expect(acquisition).to_have_text("33%")
        assert (
            authed_page.locator("#issue-1 [data-testid='series-issue-read']").get_attribute(
                "aria-label"
            )
            == "Continue Batman #1"
        )

    def test_issue_polling_pauses_while_a_reading_menu_has_focus(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)
        menu = authed_page.locator("#issue-1 [data-testid='series-issue-reading-menu']")
        trigger = menu.locator("button").first

        trigger.click()
        expect(trigger).to_be_focused()
        assert authed_page.evaluate("window.pullboxSeriesIssuesCanPoll()") is False
        authed_page.wait_for_timeout(3200)

        expect(trigger).to_be_focused()
        expect(menu.locator("[role='menu']")).to_be_visible()

    def test_reading_menu_updates_the_row_and_series_summary_in_place(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        seeded_reader_state_guard: None,
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)
        row = authed_page.locator("#issue-1")
        summary = authed_page.locator("[data-testid='series-reading-summary']").first

        row.locator("[data-testid='series-issue-reading-menu'] > button").click()
        row.get_by_role("menuitem", name="Mark read").click()

        refreshed_row = authed_page.locator("#issue-1")
        expect(refreshed_row.locator("[data-testid='series-issue-reading']")).to_have_text("Read")
        expect(summary).to_have_text("Read 1 of 1 readable")

        refreshed_row.locator("[data-testid='series-issue-reading-menu'] > button").click()
        refreshed_row.get_by_role("menuitem", name="Mark unread").click()

        restored_row = authed_page.locator("#issue-1")
        expect(restored_row.locator("[data-testid='series-issue-reading']")).to_contain_text(
            "Page 2/3"
        )
        expect(summary).to_have_text("Read 0 of 1 readable")

    def test_monitor_control_matches_action_button_height_and_shows_single_active_label(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        monitor_box = series.monitor_control.bounding_box()
        refresh_box = series.refresh_button.bounding_box()

        assert monitor_box is not None
        assert refresh_box is not None
        assert abs(monitor_box["height"] - refresh_box["height"]) <= 2
        assert series.monitor_label.text_content() == "Monitored"
        assert "Paused" not in (series.monitor_control.text_content() or "")

    def test_unmonitored_series_renders_monitor_switch_off(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(2)

        assert series.monitor_label.text_content() == "Monitored"
        assert not series.monitor_toggle.is_checked()

    def test_monitoring_toggle_updates_in_place_without_reloading_the_detail_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(2)
        authed_page.evaluate(
            """() => {
                document.querySelector("[data-testid='series-detail-page']")
                    ?.setAttribute("data-monitor-toggle-shell", "preserved");
            }"""
        )

        with authed_page.expect_response(
            lambda response: "/htmx/series/2/issues" in response.url and response.ok,
            timeout=5000,
        ):
            series.monitor_toggle.locator("xpath=..").click()

        assert authed_page.url.endswith("/series/2")
        assert series.page_shell.get_attribute("data-monitor-toggle-shell") == "preserved"
        assert series.monitor_toggle.is_checked()
        status_row = authed_page.locator("[data-testid='series-detail-status-row']")
        assert "Monitored" in (status_row.text_content() or "")

        # Restore the session-scoped seed so later E2E tests retain their fixture contract.
        with authed_page.expect_response(
            lambda response: "/htmx/series/2/issues" in response.url and response.ok,
            timeout=5000,
        ):
            series.monitor_toggle.locator("xpath=..").click()

        assert not series.monitor_toggle.is_checked()

    def test_status_row_uses_real_pill_contracts(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        status_contract = authed_page.evaluate(
            """() => {
                const row = document.querySelector("[data-testid='series-detail-status-row']");
                const pills = row ? Array.from(row.querySelectorAll(".pill")) : [];
                const first = pills[0];
                const monitored = pills.find((el) => (el.textContent || '').includes('Monitored'));
                if (!first || !monitored) {
                    return {
                        count: pills.length,
                        firstBackground: null,
                        firstFontFamily: null,
                        monitoredHasBaseClass: false,
                    };
                }
                const firstStyle = window.getComputedStyle(first);
                return {
                    count: pills.length,
                    firstBackground: firstStyle.backgroundColor,
                    firstFontFamily: firstStyle.fontFamily,
                    monitoredHasBaseClass: monitored.classList.contains('pill'),
                };
            }"""
        )

        assert status_contract["count"] >= 4
        assert status_contract["firstBackground"] not in ("rgba(0, 0, 0, 0)", "transparent")
        assert "DM Sans" in status_contract["firstFontFamily"]
        assert status_contract["monitoredHasBaseClass"] is True

    def test_sections_share_a_wider_aligned_content_width(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        geometry = authed_page.evaluate(
            """() => {
                const hero = document.querySelector("[data-testid='series-detail-hero']");
                const actions = document.querySelector("[data-testid='series-detail-hero-actions-panel']");
                const issues = document.querySelector("[data-testid='series-detail-issues-section']");
                const rectFor = (el) => {
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width };
                };
                return {
                    hero: rectFor(hero),
                    actions: rectFor(actions),
                    issues: rectFor(issues),
                    actionsParentTestId: actions?.closest("[data-testid='series-detail-hero']")?.dataset.testid || null,
                };
            }"""
        )

        assert geometry["hero"] is not None
        assert geometry["actions"] is not None
        assert geometry["issues"] is not None
        assert geometry["actionsParentTestId"] == "series-detail-hero"
        assert abs(geometry["hero"]["left"] - geometry["issues"]["left"]) <= 2
        assert abs(geometry["hero"]["right"] - geometry["issues"]["right"]) <= 2
        assert geometry["hero"]["width"] >= 1200
        assert geometry["actions"]["left"] > geometry["hero"]["left"] + 360
        assert geometry["actions"]["right"] <= geometry["hero"]["right"] + 1
        assert geometry["actions"]["top"] >= geometry["hero"]["top"]
        assert geometry["actions"]["bottom"] <= geometry["hero"]["bottom"]

    def test_back_link_returns_to_series_index_without_shell_blank(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.open_back_link()

        assert "/series" in authed_page.url
        assert authed_page.locator("[data-testid='series-page']").first.is_visible()
        assert authed_page.locator("[data-testid='page-footer-dock']").first.is_visible()

    def test_delete_returns_to_originating_series_page_and_sort(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series_list = SeriesListPage(authed_page, seeded_server)
        series_list.goto("sort=-year&per_page=2&page=2")
        series_list.open_first_series()

        back_link = authed_page.locator("[data-testid='series-detail-back-link']").first
        assert back_link.get_attribute("href") == "/series?sort=-year&per_page=2&page=2"

        authed_page.route(
            "**/htmx/series/*/delete",
            lambda route: route.fulfill(status=204),
        )
        detail = SeriesDetailPage(authed_page, seeded_server)
        detail.open_delete_modal()
        authed_page.locator("[data-testid='series-delete-submit']").first.click()

        authed_page.wait_for_url("**/series?sort=-year&per_page=2&page=2", timeout=5000)
        series_list.wait_until_ready()

        assert series_list.query_param("sort") == "-year"
        assert series_list.query_param("per_page") == "2"
        assert series_list.query_param("page") == "2"

    def test_back_link_returns_to_pull_list_when_opened_from_pull_list(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.goto(f"{seeded_server}/pull-list")
        series_link = authed_page.locator("[data-testid='pull-list-series-link']").first
        series_link.wait_for(state="visible", timeout=5000)

        series_link.click()
        authed_page.wait_for_url(
            re.compile(r"/series/\d+\?from=pull-list&return_to="),
            timeout=5000,
        )
        back_link = authed_page.locator("[data-testid='series-detail-back-link']").first

        assert back_link.text_content().strip() == "Back to pull list"
        assert back_link.get_attribute("href") == (
            "/pull-list?filter=&search=&sort=title&page=1&per_page=25"
        )

        back_link.click()
        authed_page.wait_for_url(re.compile(r"/pull-list\?"), timeout=5000)
        assert authed_page.url.endswith("/pull-list?filter=&search=&sort=title&page=1&per_page=25")
        assert authed_page.locator("[data-testid='pull-list-page']").first.is_visible()

    def test_breadcrumb_restores_series_per_page_selection(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series_list = SeriesListPage(authed_page, seeded_server)
        series_list.goto("per_page=100")
        series_list.open_first_series()

        breadcrumb_link = authed_page.locator("[data-testid='series-detail-breadcrumbs'] a").first
        assert breadcrumb_link.get_attribute("data-series-index-link") == "true"

        breadcrumb_link.click()
        authed_page.wait_for_url("**/series**", timeout=5000)
        series_list.wait_until_ready()
        authed_page.locator("[data-testid='series-per-page-select']").first.wait_for(
            state="visible", timeout=5000
        )

        assert series_list.query_param("per_page") == "100"
        assert series_list.selected_value("per_page") == "100"
        assert series_list.selected_label("per_page") == "100"

    def test_tab_switch_keeps_series_detail_content_visible(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.round_trip_tab_visibility()

        assert series.page_shell.is_visible()
        assert series.hero.is_visible()
        assert series.issues_section.is_visible()
        assert series.footer.is_visible()
        assert (
            authed_page.locator("#content").first.get_attribute("data-detail-history-hidden")
            is None
        )

    def test_delete_modal_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.open_delete_modal()

        assert series.page_shell.is_visible()
        assert series.hero.is_visible()
        assert series.delete_modal.is_visible()
        assert authed_page.get_by_test_id("series-delete-warning-row").is_visible()
        assert authed_page.get_by_test_id("series-delete-summary").is_visible()
        assert (
            authed_page.get_by_test_id("series-delete-options-header").inner_text().strip()
            == "Options"
        )
        assert authed_page.get_by_test_id("series-delete-options-panel").is_visible()
        option_spacing = authed_page.evaluate(
            """() => {
                const panel = document.querySelector("[data-testid='series-delete-options-panel']");
                const options = Array.from(document.querySelectorAll(".series-delete-modal__option"));
                if (!panel || options.length === 0) {
                    return null;
                }
                const panelRect = panel.getBoundingClientRect();
                const lastRect = options[options.length - 1].getBoundingClientRect();
                return Math.round(panelRect.bottom - lastRect.bottom);
            }"""
        )
        assert option_spacing is not None
        assert option_spacing >= 12

        series.close_delete_modal()

        assert series.page_shell.is_visible()
        assert series.footer.is_visible()

    def test_delete_folder_option_forces_delete_files(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.open_delete_modal()

        assert authed_page.get_by_test_id("series-delete-folders").is_visible()
        if authed_page.locator("[data-testid='series-delete-files']").count() == 0:
            series.close_delete_modal()
            return

        assert not series.delete_files_checked()
        assert not series.delete_files_disabled()

        series.toggle_delete_folders()

        assert series.delete_folder_checked()
        assert series.delete_files_checked()
        assert series.delete_files_disabled()

        series.close_delete_modal()

    def test_manual_search_modal_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.open_manual_search_modal()

        assert series.page_shell.is_visible()
        assert series.hero.is_visible()
        assert series.search_modal.is_visible()

        series.close_manual_search_modal()

        assert series.page_shell.is_visible()
        assert series.footer.is_visible()

    def test_manual_search_modal_hides_hover_tooltip(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        trigger = authed_page.locator("[data-testid='series-issue-manual-search']").first
        trigger.hover()

        authed_page.wait_for_function(
            """() => document.getElementById("global-tooltip-host")?.dataset.visible === "true" """
        )
        assert (
            authed_page.locator("#global-tooltip-host .app-tooltip-overlay").first.text_content()
            == "Manual search"
        )

        trigger.click()
        series.search_modal.wait_for(state="visible", timeout=5000)

        authed_page.wait_for_function(
            """() => !document.getElementById("global-tooltip-host")?.dataset.visible"""
        )
        assert not authed_page.locator(
            "#global-tooltip-host .app-tooltip-overlay"
        ).first.is_visible()

    def test_issue_status_filter_keeps_series_detail_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.select_issue_status_filter("wanted")

        assert series.page_shell.is_visible()
        assert series.hero.is_visible()
        assert series.issues_section.is_visible()
        assert series.footer.is_visible()
        assert series.dropdown_label("series-detail-issues-status-select") == "Wanted"
        assert authed_page.locator("[data-testid='series-detail-issues-table']").first.is_visible()

    def test_issue_rows_do_not_expose_inline_status_toggle_controls(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        assert series.issues_section.is_visible()
        assert authed_page.locator("[data-testid='series-issue-row']").count() > 0
        assert authed_page.locator("[data-testid='series-issue-status-toggle']").count() == 0

    def test_issue_status_filter_empty_state_has_no_console_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        console_errors: list[str] = []
        page_errors: list[str] = []
        authed_page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        authed_page.on("pageerror", lambda error: page_errors.append(str(error)))

        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.select_issue_status_filter("downloading")

        assert series.page_shell.is_visible()
        assert series.hero.is_visible()
        assert series.issues_section.is_visible()
        assert series.footer.is_visible()
        assert series.dropdown_label("series-detail-issues-status-select") == "Downloading"
        assert authed_page.locator("[data-testid='series-detail-issues-table']").first.is_visible()
        assert authed_page.locator('text=No issues with status "downloading".').first.is_visible()

        real_console_errors = [
            error
            for error in console_errors
            if "favicon" not in error.lower() and "404" not in error
        ]
        assert real_console_errors == []
        assert page_errors == []

    def test_issue_status_dropdown_remains_attached_in_empty_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1280, "height": 700})
        series = SeriesDetailPage(authed_page, seeded_server)
        series.goto(1)

        series.select_issue_status_filter("downloading")

        issues_status = authed_page.locator(
            "[data-testid='series-detail-issues-status-select']"
        ).first
        trigger = issues_status.locator("[data-dropdown-select-trigger]").first
        trigger.scroll_into_view_if_needed()
        trigger.click()

        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first
        panel.wait_for(state="visible", timeout=5000)

        trigger_box = trigger.bounding_box()
        panel_box = panel.bounding_box()

        assert trigger_box is not None
        assert panel_box is not None
        assert panel_box["y"] >= 0
        assert panel_box["y"] + panel_box["height"] <= 700
        assert abs(panel_box["x"] - trigger_box["x"]) <= 2
