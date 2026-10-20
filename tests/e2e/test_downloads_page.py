"""Focused browser coverage for the rewritten downloads page."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import run_htmx_ajax_and_wait, wait_for_htmx
from tests.e2e.pages.downloads import DownloadsPage

pytestmark = pytest.mark.e2e


def _install_downloads_history_timer_capture(page) -> None:  # type: ignore[no-untyped-def]
    page.add_init_script(
        """
        () => {
          const originalSetTimeout = window.setTimeout.bind(window);
          const originalClearTimeout = window.clearTimeout.bind(window);
          const originalSetInterval = window.setInterval.bind(window);
          const originalClearInterval = window.clearInterval.bind(window);
          window.__pbDownloadsHistoryTimers = [];

          function capture(kind, fn, delay, args) {
            const id = window.__pbDownloadsHistoryTimers.length + 1;
            window.__pbDownloadsHistoryTimers.push({ id, fn, delay, args, kind, cleared: false });
            return id;
          }

          window.setTimeout = function (fn, delay, ...args) {
            if (delay === 3000) {
              return capture("timeout", fn, delay, args);
            }
            return originalSetTimeout(fn, delay, ...args);
          };

          window.clearTimeout = function (id) {
            const entry = (window.__pbDownloadsHistoryTimers || []).find((item) => item.id === id);
            if (entry) {
              entry.cleared = true;
              return;
            }
            return originalClearTimeout(id);
          };

          window.setInterval = function (fn, delay, ...args) {
            if (delay === 3000) {
              return capture("interval", fn, delay, args);
            }
            return originalSetInterval(fn, delay, ...args);
          };

          window.clearInterval = function (id) {
            const entry = (window.__pbDownloadsHistoryTimers || []).find((item) => item.id === id);
            if (entry) {
              entry.cleared = true;
              return;
            }
            return originalClearInterval(id);
          };
        }
        """
    )


def _run_downloads_history_poll_tick(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """
        () => {
          for (const entry of window.__pbDownloadsHistoryTimers || []) {
            if (entry.delay === 3000 && !entry.cleared) {
              entry.fn(...entry.args);
            }
          }
        }
        """
    )


class TestDownloadsPage:
    """Behavior-first E2E checks for the downloads shell."""

    @pytest.mark.parametrize("theme", ["light", "dark", "tron"])
    def test_active_download_size_is_visible_and_refreshes(
        self, authed_page, seeded_server: str, monkeypatch, theme: str
    ) -> None:  # type: ignore[no-untyped-def]
        page = authed_page
        page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
        progress = AsyncMock(return_value={})
        monkeypatch.setattr("pullbox.ui.routes._load_download_progress_map", progress)
        page.set_viewport_size({"width": 1440, "height": 900})
        downloads = DownloadsPage(page, seeded_server)
        downloads.goto()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        table = page.get_by_test_id("downloads-queue-active-table")
        expect(table.get_by_role("columnheader", name="Size", exact=True)).to_be_visible()
        expect(table.get_by_test_id("downloads-queue-item-size")).to_have_text("100.0 MB")

        async def live_progress(_session, queue_items, **_kwargs):  # type: ignore[no-untyped-def]
            return {
                item.id: SimpleNamespace(progress=0.5, size_bytes=209_715_200)
                for item in queue_items
            }

        progress.side_effect = live_progress
        page.evaluate("htmx.trigger(document.body, 'downloads:refresh')")
        expect(table.get_by_test_id("downloads-queue-item-size")).to_have_text("200.0 MB")
        assert_no_axe_violations(page, name=f"download-size-{theme}", include=["#content"])
        output = Path("test-results/download-size")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f"{theme}.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("#content").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        table.get_by_test_id("downloads-queue-item-size").scroll_into_view_if_needed()
        expect(table.get_by_test_id("downloads-queue-item-size")).to_be_visible()
        page.screenshot(path=str(output / f"{theme}-mobile.png"), full_page=True)

    def test_downloads_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        assert downloads.page_root.is_visible()
        assert downloads.header.is_visible()
        assert downloads.gauges.is_visible()
        assert downloads.tabs.is_visible()
        assert downloads.content.is_visible()
        assert downloads.queue_panel.is_visible()
        assert downloads.footer_dock.is_visible()
        assert downloads.tab("queue").get_attribute("aria-current") == "page"
        assert downloads.queue_active_section.is_visible()
        assert downloads.queue_waiting_section.is_visible()
        assert (
            authed_page.locator("[data-testid='downloads-queue-active-table']").count() == 1
            or downloads.queue_active_empty.is_visible()
        )
        assert (
            authed_page.locator("[data-testid='downloads-queue-waiting-table']").count() == 1
            or downloads.queue_waiting_empty.is_visible()
        )
        assert (
            authed_page.locator("[data-testid='downloads-queue-item-details-toggle']").count() == 0
        )

    def test_downloads_tab_switch_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        downloads.switch_tab("history")

        assert downloads.page_root.is_visible()
        assert authed_page.locator("[data-testid='downloads-page']").count() == 1
        assert authed_page.locator("[data-testid='downloads-content']").count() == 1
        assert downloads.header.is_visible()
        assert downloads.gauges.is_visible()
        assert authed_page.locator("[data-testid='downloads-gauges'] .downloads-gauge").count() == 3
        assert downloads.history_panel.is_visible()
        assert downloads.history_toolbar.is_visible()
        assert downloads.history_table.is_visible()
        assert downloads.footer_dock.is_visible()
        assert downloads.history_clear_button.is_visible()
        assert downloads.history_search_input.is_visible()
        assert downloads.tab("history").get_attribute("aria-current") == "page"
        assert downloads.history_item("Batman 001").is_visible()

    def test_downloads_tabs_share_the_header_action_rail_pattern(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        downloads_header_box = downloads.header.bounding_box()
        downloads_tabs_box = downloads.tabs.bounding_box()

        assert downloads_header_box is not None
        assert downloads_tabs_box is not None

        downloads_offset = downloads_tabs_box["y"] - downloads_header_box["y"]

        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle")

        series_header = authed_page.locator("[data-testid='series-registry-header']").first
        series_actions = authed_page.locator("[data-testid='series-registry-actions']").first
        series_header_box = series_header.bounding_box()
        series_actions_box = series_actions.bounding_box()

        assert series_header_box is not None
        assert series_actions_box is not None

        series_offset = series_actions_box["y"] - series_header_box["y"]

        assert abs(downloads_offset - series_offset) <= 10

    def test_downloads_queue_header_matches_series_header_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        downloads_title = authed_page.locator(
            "[data-testid='downloads-header'] .downloads-title"
        ).first
        downloads_gauge = authed_page.locator(
            "[data-testid='downloads-gauges'] .downloads-gauge-ring"
        ).first

        downloads_title_style = downloads_title.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                lineHeight: style.lineHeight,
              };
            }
            """
        )
        downloads_gauge_box = downloads_gauge.bounding_box()

        assert downloads_gauge_box is not None

        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle")

        series_title = authed_page.locator("[data-testid='series-registry-title']").first
        series_gauge = authed_page.locator(
            "[data-testid='series-registry-gauges'] .series-registry-gauge-ring"
        ).first

        series_title_style = series_title.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                lineHeight: style.lineHeight,
              };
            }
            """
        )
        series_gauge_box = series_gauge.bounding_box()

        assert series_gauge_box is not None
        assert downloads_title_style == series_title_style
        assert abs(downloads_gauge_box["width"] - series_gauge_box["width"]) <= 1
        assert abs(downloads_gauge_box["height"] - series_gauge_box["height"]) <= 1

    def test_downloads_header_top_offset_matches_series_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        app_header = authed_page.locator("[data-testid='app-header']").first
        downloads_header = downloads.header

        app_header_box = app_header.bounding_box()
        downloads_header_box = downloads_header.bounding_box()

        assert app_header_box is not None
        assert downloads_header_box is not None

        downloads_offset = downloads_header_box["y"] - (
            app_header_box["y"] + app_header_box["height"]
        )

        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle")

        series_header = authed_page.locator("[data-testid='series-registry-header']").first
        series_header_box = series_header.bounding_box()

        assert series_header_box is not None

        series_offset = series_header_box["y"] - (app_header_box["y"] + app_header_box["height"])

        assert abs(downloads_offset - series_offset) <= 2

    def test_downloads_history_search_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.search_history("Action")

        assert downloads.page_root.is_visible()
        assert authed_page.locator("[data-testid='downloads-content']").count() == 1
        assert downloads.history_panel.is_visible()
        assert downloads.history_empty.is_visible()
        assert "search=Action" in authed_page.url
        assert downloads.history_search_input.input_value() == "Action"

    def test_downloads_history_search_submits_on_input_without_enter(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.history_search_input.press_sequentially("Batman")
        authed_page.wait_for_url("**/downloads?tab=history**search=Batman**", timeout=6000)

        assert "search=Batman" in authed_page.url
        assert downloads.history_panel.is_visible()
        assert downloads.history_item("Batman 001").is_visible()

    def test_downloads_history_search_stays_focused_like_series_toolbar(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _install_downloads_history_timer_capture(authed_page)
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.history_search_input.click()

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is False

        downloads.history_search_input.press_sequentially("Batman")
        authed_page.wait_for_url("**/downloads?tab=history**search=Batman**", timeout=6000)
        _run_downloads_history_poll_tick(authed_page)

        assert downloads.history_search_input.input_value() == "Batman"
        assert (
            authed_page.evaluate("""() => document.activeElement?.getAttribute("data-testid")""")
            == "downloads-history-search"
        )

    def test_downloads_history_dropdown_stays_open_until_selection_or_clickaway(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _install_downloads_history_timer_capture(authed_page)
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        status_root = downloads.history_status_filter
        status_root.locator("[data-dropdown-select-trigger]").click()
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first
        panel.wait_for(state="visible", timeout=5000)

        dropdown_overlay_state = authed_page.evaluate(
            """
            () => {
              const panel = Array.from(document.querySelectorAll("[data-dropdown-select-panel]"))
                .find((node) => {
                  const style = window.getComputedStyle(node);
                  return style.display !== "none" && style.visibility !== "hidden";
                });
              if (!panel) {
                return null;
              }
              const rect = panel.getBoundingClientRect();
              const probePoints = [
                { x: rect.left + 18, y: rect.top + 18 },
                { x: rect.left + 18, y: rect.top + 52 },
                { x: rect.left + 18, y: rect.bottom - 18 },
              ];
              return {
                parentTag: panel.parentElement?.tagName?.toLowerCase() ?? null,
                style: {
                  position: window.getComputedStyle(panel).position,
                  zIndex: window.getComputedStyle(panel).zIndex,
                },
                probeResults: probePoints.map(({ x, y }) => {
                  const hit = document.elementFromPoint(x, y);
                  return {
                    inPanel: Boolean(hit?.closest("[data-dropdown-select-panel]")),
                    text: (hit?.textContent ?? "").trim().slice(0, 40),
                  };
                }),
              };
            }
            """
        )

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is False
        assert dropdown_overlay_state["parentTag"] == "body"
        assert dropdown_overlay_state["style"] == {
            "position": "fixed",
            "zIndex": "80",
        }
        assert all(probe["inPanel"] is True for probe in dropdown_overlay_state["probeResults"])
        assert dropdown_overlay_state["probeResults"][-1]["text"] == "Cancelled"

        _run_downloads_history_poll_tick(authed_page)

        assert panel.is_visible()

        authed_page.locator("body").click(position={"x": 10, "y": 10})

        panel.wait_for(state="hidden", timeout=5000)

    def test_downloads_history_filter_label_click_does_not_toggle_dropdown(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        client_filter = downloads.history_client_filter
        trigger = client_filter.locator("[data-dropdown-select-trigger]").first
        label = (
            client_filter.locator(
                "xpath=ancestor::div[contains(@class, 'series-toolbar-field')][1]"
            )
            .locator(".series-toolbar-label")
            .first
        )

        label.click()

        assert trigger.get_attribute("aria-expanded") == "false"
        assert authed_page.locator("[data-dropdown-select-panel]:visible").count() == 0

    def test_downloads_history_dropdown_width_stays_stable_after_open(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.history_client_filter.locator("[data-dropdown-select-trigger]").click()
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first
        panel.wait_for(state="visible", timeout=5000)

        widths = authed_page.evaluate(
            """
            async () => {
              const panel = Array.from(document.querySelectorAll("[data-dropdown-select-panel]"))
                .find((node) => {
                  const style = window.getComputedStyle(node);
                  return style.display !== "none" && style.visibility !== "hidden";
                });
              if (!panel) {
                throw new Error("Visible dropdown panel not found");
              }
              const values = [];
              const capture = () => values.push(Math.round(panel.getBoundingClientRect().width));
              capture();
              await new Promise((resolve) => requestAnimationFrame(resolve));
              capture();
              await new Promise((resolve) => requestAnimationFrame(resolve));
              capture();
              await new Promise((resolve) => setTimeout(resolve, 120));
              capture();
              return values;
            }
            """
        )

        assert max(widths) - min(widths) <= 1

    def test_downloads_history_dropdown_options_stay_single_line(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.history_status_filter.locator("[data-dropdown-select-trigger]").click()
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first
        panel.wait_for(state="visible", timeout=5000)

        option_metrics = authed_page.evaluate(
            """
            () => {
              const panel = Array.from(document.querySelectorAll("[data-dropdown-select-panel]"))
                .find((node) => {
                  const style = window.getComputedStyle(node);
                  return style.display !== "none" && style.visibility !== "hidden";
                });
              if (!panel) {
                throw new Error("Visible dropdown panel not found");
              }
              return Array.from(panel.querySelectorAll(".dropdown-select-option-label")).map((label) => {
                const style = window.getComputedStyle(label);
                return {
                  text: (label.textContent || "").trim(),
                  whiteSpace: style.whiteSpace,
                  wraps: label.getClientRects().length > 1,
                  clipped: label.scrollWidth > label.clientWidth + 1,
                };
              });
            }
            """
        )

        assert option_metrics
        assert all(metric["whiteSpace"] == "nowrap" for metric in option_metrics)
        assert all(metric["wraps"] is False for metric in option_metrics)
        assert all(metric["clipped"] is False for metric in option_metrics)

    def test_downloads_history_toolbar_keeps_attached_top_corner_radius(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        radii = downloads.history_toolbar.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                topLeft: style.borderTopLeftRadius,
                topRight: style.borderTopRightRadius,
                bottomLeft: style.borderBottomLeftRadius,
                bottomRight: style.borderBottomRightRadius,
              };
            }
            """
        )

        assert radii == {
            "topLeft": "14px",
            "topRight": "14px",
            "bottomLeft": "0px",
            "bottomRight": "0px",
        }

    def test_downloads_queue_poll_keeps_header_and_rows_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        assert downloads.header.is_visible()
        assert authed_page.locator("[data-testid='downloads-queue-panel']").count() == 1

        run_htmx_ajax_and_wait(authed_page, "/htmx/downloads/queue", "#downloads-content")

        assert downloads.header.is_visible()
        assert authed_page.locator("[data-testid='downloads-queue-panel']").count() == 1
        assert downloads.footer_dock.is_visible()

    @pytest.mark.parametrize("response_changed", [False, True], ids=["unchanged", "changed"])
    def test_downloads_queue_cancel_remains_interactive_after_poll(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        response_changed: bool,
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        refreshed_html = downloads.content.evaluate(
            """(node, responseChanged) => {
                const refreshed = node.cloneNode(true);
                if (responseChanged) {
                    refreshed.dataset.pollRevision = "changed";
                }
                return refreshed.outerHTML;
            }""",
            response_changed,
        )

        authed_page.route(
            "**/htmx/downloads/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=refreshed_html,
            ),
            times=1,
        )
        run_htmx_ajax_and_wait(authed_page, "/htmx/downloads/queue", "#downloads-content")
        authed_page.wait_for_function(
            """() => {
                const button = document.querySelector(
                    "[data-testid^='downloads-queue-cancel-']"
                );
                return Boolean(button && button._x_marker);
            }""",
            timeout=5000,
        )

        authed_page.locator("[data-testid^='downloads-queue-cancel-']").first.click()

        confirm_dialog = authed_page.locator("#pb-confirm-dialog")
        expect(confirm_dialog.locator("#pb-confirm-title")).to_have_text("Cancel Download")
        expect(confirm_dialog.locator("[x-show='visible']").first).to_be_visible()
        confirm_dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(confirm_dialog.locator("[x-show='visible']").first).to_be_hidden()

    def test_downloads_history_filters_and_queue_poll_keep_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.select_history_status("cancelled")
        wait_for_htmx(authed_page)

        assert downloads.page_root.is_visible()
        assert authed_page.locator("[data-testid='downloads-content']").count() == 1
        assert downloads.history_empty.is_visible()

        downloads.switch_tab("queue")
        run_htmx_ajax_and_wait(authed_page, "/htmx/downloads/queue", "#downloads-content")

        assert downloads.page_root.is_visible()
        assert authed_page.locator("[data-testid='downloads-queue-panel']").count() == 1
        assert downloads.footer_dock.is_visible()
        assert (
            authed_page.locator("[data-testid='downloads-queue-item-details-toggle']").count() == 0
        )

    def test_downloads_history_poll_preserves_active_filters(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        downloads.select_history_status("cancelled")
        wait_for_htmx(authed_page)

        assert downloads.history_empty.is_visible()
        assert "status=cancelled" in (
            downloads.history_panel.locator(
                "[data-testid='downloads-history-results']"
            ).get_attribute("hx-get")
            or ""
        )

        authed_page.evaluate(
            """() => {
                const target = document.querySelector('#downloads-history-results');
                if (!target) throw new Error('downloads history results missing');
                htmx.trigger(target, 'refresh');
            }"""
        )
        wait_for_htmx(authed_page)

        assert downloads.history_empty.is_visible()
        assert downloads.history_table.count() == 0

    def test_downloads_history_refresh_gate_pauses_when_detail_is_open(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is True

    def test_downloads_history_error_detail_toggle_reopens_cleanly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        toggle = downloads.first_history_error_toggle
        detail = downloads.first_history_error_detail
        detail_rows = downloads.history_panel.locator(
            "[data-testid='downloads-history-error-detail-content']"
        )

        assert toggle.get_attribute("aria-expanded") == "false"
        expect(detail_rows).to_have_count(0)

        toggle.click()
        wait_for_htmx(authed_page)
        detail.wait_for(state="visible", timeout=5000)
        assert toggle.get_attribute("aria-expanded") == "true"

        toggle.click()
        wait_for_htmx(authed_page)
        expect(detail_rows).to_have_count(0)
        assert toggle.get_attribute("aria-expanded") == "false"

        toggle.click()
        wait_for_htmx(authed_page)
        detail.wait_for(state="visible", timeout=5000)
        assert toggle.get_attribute("aria-expanded") == "true"

    def test_downloads_history_lazy_detail_clicks_stay_in_sync(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        result = authed_page.evaluate(
            """
            () => {
              const button = document.createElement('button');
              const rowId = 'downloads-history-error-row-test';
              const triggerName = 'pullbox-download-history-detail-test';
              const states = [];
              let loadEvents = 0;

              window.addEventListener('pb-lazy-table-detail-state', (event) => {
                if (event.detail.rowId === rowId) {
                  states.push(event.detail.expanded);
                }
              });
              document.body.addEventListener(triggerName, () => {
                loadEvents += 1;
              });

              const first = window.pbToggleLazyTableDetail(button, rowId, triggerName);
              const loadedRow = document.createElement('tr');
              loadedRow.id = rowId;
              document.body.appendChild(loadedRow);
              const second = window.pbToggleLazyTableDetail(button, rowId, triggerName);
              const third = window.pbToggleLazyTableDetail(button, rowId, triggerName);

              document.getElementById(rowId)?.remove();

              return {
                first,
                second,
                third,
                loadEvents,
                states,
                ariaExpanded: button.getAttribute('aria-expanded'),
              };
            }
            """
        )

        assert result == {
            "first": True,
            "second": False,
            "third": True,
            "loadEvents": 2,
            "states": [True, False, True],
            "ariaExpanded": "true",
        }

    def test_poll_swap_is_blocked_while_lazy_detail_wants_to_remain_open(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        should_swap = authed_page.evaluate(
            """
            () => {
              const target = document.createElement('div');
              target.id = 'lazy-detail-poll-race-test';
              target.setAttribute('hx-trigger', 'every 3s');

              const button = document.createElement('button');
              button.dataset.lazyDetailDesiredOpen = 'true';
              target.appendChild(button);
              document.body.appendChild(target);

              const detail = {
                elt: target,
                serverResponse: '<div id="lazy-detail-poll-race-test">updated</div>',
                shouldSwap: true,
              };
              target.dispatchEvent(
                new CustomEvent('htmx:beforeSwap', {
                  bubbles: true,
                  detail,
                })
              );
              target.remove();
              return detail.shouldSwap;
            }
            """
        )

        assert should_swap is False

    def test_downloads_history_refresh_gate_pauses_when_results_are_hovered(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        row = downloads.history_table.locator("tbody tr").first
        row.hover()
        authed_page.wait_for_function(
            "() => window.downloadsHistoryRefreshEnabled() === false",
            timeout=5000,
        )

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is False

        authed_page.locator("[data-testid='downloads-header']").hover()
        authed_page.wait_for_function(
            "() => window.downloadsHistoryRefreshEnabled() === true",
            timeout=5000,
        )

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is True

    def test_downloads_history_rows_use_shared_selected_surface_hover(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        row = downloads.history_table.locator("tbody tr").first
        cell = row.locator("td").first

        expected_hover_bg = authed_page.evaluate(
            """
            () => {
              const probe = document.createElement('div');
              probe.style.background = 'var(--pb-surface-selected)';
              probe.style.position = 'absolute';
              probe.style.visibility = 'hidden';
              document.body.appendChild(probe);
              const value = window.getComputedStyle(probe).backgroundColor;
              probe.remove();
              return value;
            }
            """
        )
        before_hover = cell.evaluate("node => window.getComputedStyle(node).backgroundColor")
        row.hover()
        authed_page.wait_for_timeout(100)
        after_hover = cell.evaluate("node => window.getComputedStyle(node).backgroundColor")

        assert after_hover == expected_hover_bg
        assert after_hover != before_hover

    def test_downloads_hover_only_tints_the_hovered_row(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto(tab="history")

        authed_page.evaluate(
            """
            () => {
              const host = document.createElement('div');
              host.id = 'downloads-hover-probe';
              host.innerHTML = `
                <div class="downloads-table-wrap">
                  <table class="downloads-table">
                    <tbody>
                      <tr data-probe-row="one"><td>Row One</td></tr>
                      <tr data-probe-row="two"><td>Row Two</td></tr>
                    </tbody>
                  </table>
                </div>
              `;
              document.body.appendChild(host);
            }
            """
        )

        expected_hover_bg = authed_page.evaluate(
            """
            () => {
              const probe = document.createElement('div');
              probe.style.background = 'var(--pb-surface-selected)';
              probe.style.position = 'absolute';
              probe.style.visibility = 'hidden';
              document.body.appendChild(probe);
              const value = window.getComputedStyle(probe).backgroundColor;
              probe.remove();
              return value;
            }
            """
        )

        hovered_cell = authed_page.locator("[data-probe-row='one'] td").first
        sibling_cell = authed_page.locator("[data-probe-row='two'] td").first

        sibling_before = sibling_cell.evaluate(
            "node => window.getComputedStyle(node).backgroundColor"
        )

        authed_page.locator("[data-probe-row='one']").hover()
        authed_page.wait_for_timeout(100)

        hovered_after = hovered_cell.evaluate(
            "node => window.getComputedStyle(node).backgroundColor"
        )
        sibling_after = sibling_cell.evaluate(
            "node => window.getComputedStyle(node).backgroundColor"
        )

        assert hovered_after == expected_hover_bg
        assert sibling_after == sibling_before

    def test_downloads_history_headers_match_queue_header_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        queue_header = authed_page.locator(
            "[data-testid='downloads-content'] table.downloads-table thead th"
        ).nth(1)

        downloads.switch_tab("history")

        history_header = authed_page.locator("[data-testid='downloads-history-sort-title']").first

        queue_style = queue_header.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                textTransform: style.textTransform,
              };
            }
            """
        )
        history_style = history_header.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                textTransform: style.textTransform,
              };
            }
            """
        )

        assert history_style == queue_style
        assert history_style["textTransform"] == "uppercase"

    def test_downloads_headers_match_series_table_header_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        downloads_header = authed_page.locator(
            "[data-testid='downloads-content'] table.downloads-table thead th"
        ).nth(1)
        downloads_style = downloads_header.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                textTransform: style.textTransform,
              };
            }
            """
        )

        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle")

        series_header = authed_page.locator(".series-mission-control-table thead th").nth(1)

        series_style = series_header.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                textTransform: style.textTransform,
              };
            }
            """
        )
        assert downloads_style == series_style

    def test_downloads_uses_full_content_rail_on_wide_viewports(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        downloads = DownloadsPage(authed_page, seeded_server)
        downloads.goto()

        content_box = authed_page.locator("#content").bounding_box()
        downloads_box = downloads.content.bounding_box()

        assert content_box is not None
        assert downloads_box is not None
        assert downloads_box["width"] >= content_box["width"] - 56

        authed_page.evaluate(
            """() => {
                const marker = document.createElement('div');
                marker.setAttribute('data-downloads-history-expanded', 'true');
                marker.id = 'downloads-history-expanded-marker';
                document.body.appendChild(marker);
            }"""
        )

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is False

        authed_page.evaluate(
            """() => {
                document.getElementById('downloads-history-expanded-marker')?.remove();
            }"""
        )

        assert authed_page.evaluate("() => window.downloadsHistoryRefreshEnabled()") is True
