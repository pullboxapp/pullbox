"""Focused E2E coverage for the standardized app sidebar shell."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import wait_for_htmx
from tests.e2e.pages.app_shell import AppShellPage
from tests.e2e.pages.import_page import ImportPage
from tests.e2e.pages.series_list import SeriesListPage

pytestmark = pytest.mark.e2e


class TestSidebarShell:
    """Behavior-first E2E checks for the persistent sidebar shell."""

    def test_sidebar_shell_recovers_when_initial_stylesheet_request_fails(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        stylesheet_requests: list[str] = []

        def fail_initial_stylesheet(route) -> None:  # type: ignore[no-untyped-def]
            stylesheet_requests.append(route.request.url)
            if len(stylesheet_requests) == 1:
                route.abort()
                return
            route.continue_()

        authed_page.route("**/static/css/tailwind.css*", fail_initial_stylesheet)

        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")
        authed_page.wait_for_function(
            "document.documentElement.hasAttribute('data-pullbox-stylesheet-ready')",
            timeout=5000,
        )

        assert len(stylesheet_requests) == 2
        assert "pullbox_retry=" in stylesheet_requests[1]
        assert authed_page.locator("body").get_attribute("data-shell-pending") is None
        assert shell.sidebar.evaluate("node => getComputedStyle(node).position") == "fixed"

    def test_sidebar_shell_renders_standardized_regions(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")

        assert authed_page.locator("body").get_attribute("data-shell-pending") is None
        assert shell.sidebar.is_visible()
        assert shell.nav.is_visible()
        assert authed_page.locator("[data-testid='sidebar-footer']").count() == 0
        assert authed_page.locator("[data-testid='sidebar-footer-status']").count() == 0
        assert shell.section("library").is_visible()
        assert shell.section("admin").is_visible()
        assert shell.add_series_button.is_visible()
        assert shell.link("series").is_visible()
        assert shell.link("import").is_visible()
        assert shell.link("health").is_visible()
        assert shell.badge("intervention").count() == 1
        assert shell.badge("health").count() == 1

    def test_global_activity_popover_renders_shared_operation_progress(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/activity",
            lambda route: route.fulfill(
                json={
                    "active_count": 1,
                    "spinner_count": 1,
                    "attention_count": 0,
                    "operations": [
                        {
                            "id": 41,
                            "operation_type": "download",
                            "operation_key": "download-41",
                            "state": "running",
                            "phase": "downloading",
                            "title": "Batman 041",
                            "message": "Receiving from PixelDrain",
                            "source_label": "PixelDrain",
                            "detail_url": "/downloads",
                            "tone": "info",
                            "attention_required": False,
                            "acknowledged_at": None,
                            "overall": {
                                "current": 40,
                                "total": 100,
                                "percent": 40,
                                "unit": "bytes",
                                "indeterminate": False,
                            },
                            "item": {
                                "key": "batman-041.cbz",
                                "label": "Batman 041.cbz",
                                "phase": "transferring",
                                "message": "Receiving",
                                "current": None,
                                "total": None,
                                "percent": None,
                                "unit": "bytes",
                                "indeterminate": True,
                            },
                            "rate": 2097152,
                            "rate_unit": "bytes_per_second",
                            "eta_seconds": 120,
                        }
                    ],
                }
            ),
        )

        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")
        activity_button = authed_page.get_by_role("button", name="Background activity")
        operation = authed_page.locator("[data-testid='header-activity-operation']")
        expect(activity_button).to_have_attribute("aria-expanded", "false")
        expect(operation).to_have_count(1)
        activity_button.click()

        popover = authed_page.locator("[data-testid='header-activity-popover']")
        expect(activity_button).to_have_attribute("aria-expanded", "true")
        expect(popover).to_be_visible()
        assert operation.get_by_text("Batman 041", exact=True).is_visible()
        assert operation.get_by_text("PixelDrain", exact=True).is_visible()
        assert operation.get_by_text("Batman 041.cbz", exact=True).is_visible()
        overall_progress = operation.locator(
            "[data-testid='header-activity-overall-progress'] [role='progressbar']:visible"
        )
        assert overall_progress.get_attribute("aria-valuenow") == "40"
        progress_track = overall_progress.bounding_box()
        progress_fill_locator = overall_progress.locator(":scope > div")
        progress_fill = progress_fill_locator.bounding_box()
        assert progress_track is not None
        assert progress_fill is not None
        assert 0.38 <= progress_fill["width"] / progress_track["width"] <= 0.42
        track_color = overall_progress.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        fill_color = progress_fill_locator.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        assert fill_color not in {"transparent", "rgba(0, 0, 0, 0)"}
        assert fill_color != track_color
        assert (
            operation.locator(
                "[data-testid='header-activity-item-progress'] [role='progressbar']:visible"
            ).get_attribute("aria-valuenow")
            is None
        )

    def test_global_activity_popover_does_not_invent_missing_eta(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        states = ("queued", "retrying", "failed")
        authed_page.route(
            "**/api/v1/activity",
            lambda route: route.fulfill(
                json={
                    "active_count": 2,
                    "spinner_count": 2,
                    "attention_count": 1,
                    "operations": [
                        {
                            "id": index,
                            "operation_type": "download",
                            "operation_key": f"download-{index}",
                            "state": state,
                            "phase": state,
                            "title": f"Download {state}",
                            "message": state.title(),
                            "source_label": "Direct download",
                            "detail_url": "/downloads",
                            "tone": "danger" if state == "failed" else "warning",
                            "attention_required": state == "failed",
                            "acknowledged_at": None,
                            "overall": {
                                "current": None,
                                "total": None,
                                "percent": None,
                                "unit": "bytes",
                                "indeterminate": True,
                            },
                            "item": None,
                            "rate": None,
                            "rate_unit": "bytes_per_second",
                            "eta_seconds": None,
                        }
                        for index, state in enumerate(states, start=51)
                    ],
                }
            ),
        )

        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")
        activity_button = authed_page.get_by_role("button", name="Background activity")
        operations = authed_page.locator("[data-testid='header-activity-operation']")
        expect(operations).to_have_count(3)
        activity_button.click()

        popover = authed_page.locator("[data-testid='header-activity-popover']")
        expect(popover).to_be_visible()
        expect(popover.get_by_text("1 sec remaining", exact=True)).to_have_count(0)

    def test_sidebar_collapse_toggle_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")

        sidebar_classes = shell.sidebar.get_attribute("class") or ""
        assert "sidebar-transition" not in sidebar_classes
        html_classes = authed_page.locator("html").get_attribute("class") or ""
        assert "shell-layout-transition" not in html_classes

        before = shell.sidebar.bounding_box()
        assert before is not None
        assert 236 <= before["width"] <= 244
        toggle_before = shell.collapse_toggle.bounding_box()
        assert toggle_before is not None
        assert toggle_before["x"] + toggle_before["width"] > before["x"] + before["width"] - 2
        assert toggle_before["x"] < before["x"] + before["width"]

        shell.collapse_toggle.evaluate("(el) => el.click()")
        authed_page.wait_for_timeout(250)
        sidebar_classes = shell.sidebar.get_attribute("class") or ""
        assert "sidebar-transition" in sidebar_classes
        html_classes = authed_page.locator("html").get_attribute("class") or ""
        assert "shell-layout-transition" in html_classes
        collapsed = shell.sidebar.bounding_box()

        assert collapsed is not None
        assert collapsed["width"] < before["width"]
        assert 68 <= collapsed["width"] <= 76
        assert shell.link("series").is_visible()
        assert not authed_page.locator("[data-sidebar-logo-label]").first.is_visible()
        assert authed_page.locator("[data-sidebar-section-label]:visible").count() == 0
        assert (
            authed_page.locator("[data-sidebar-logo-label]").evaluate(
                "(node) => window.getComputedStyle(node).display"
            )
            == "none"
        )

        brand_card = shell.logo_link.locator(".app-sidebar-brand-card").first.bounding_box()
        dashboard_link = shell.link("dashboard").bounding_box()
        series_link = shell.link("series").bounding_box()

        assert brand_card is not None
        assert dashboard_link is not None
        assert series_link is not None
        assert brand_card["width"] <= 52
        assert brand_card["height"] <= 52
        assert dashboard_link["y"] - (brand_card["y"] + brand_card["height"]) <= 32
        assert series_link["width"] <= 52

        collapsed_left = collapsed["x"]
        collapsed_right = collapsed_left + collapsed["width"]
        series_left_inset = series_link["x"] - collapsed_left
        series_right_inset = collapsed_right - (series_link["x"] + series_link["width"])

        assert series_link["x"] >= collapsed_left
        assert series_link["x"] + series_link["width"] <= collapsed_right
        assert abs(series_left_inset - series_right_inset) <= 4

        assert shell.link("series").get_attribute("data-tip") == "Series"
        assert authed_page.locator("[data-sidebar-section-rule]:visible").count() >= 1

        shell.collapse_toggle.evaluate("(el) => el.click()")
        authed_page.wait_for_timeout(250)
        expanded = shell.sidebar.bounding_box()

        assert expanded is not None
        assert expanded["width"] > collapsed["width"]

    def test_sidebar_collapse_toggle_uses_custom_tooltip_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")

        assert shell.collapse_toggle.get_attribute("data-tip") == "Collapse sidebar"
        assert shell.collapse_toggle.get_attribute("data-tip-pos") == "right"
        assert shell.collapse_toggle.get_attribute("title") is None

        shell.collapse_toggle.evaluate("(el) => el.click()")
        authed_page.wait_for_timeout(250)

        assert shell.collapse_toggle.get_attribute("data-tip") == "Expand sidebar"
        assert shell.collapse_toggle.get_attribute("data-tip-pos") == "right"
        assert shell.collapse_toggle.get_attribute("title") is None

    def test_boosted_sidebar_navigation_updates_active_link(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")

        shell.link("downloads").click()
        wait_for_htmx(authed_page)
        authed_page.wait_for_function(
            """() => {
                const downloads = document.querySelector("[data-testid='sidebar-link-downloads']");
                const series = document.querySelector("[data-testid='sidebar-link-series']");
                if (!downloads || !series) {
                    return false;
                }
                const downloadsClasses = downloads.getAttribute("class") || "";
                const seriesClasses = series.getAttribute("class") || "";
                return downloadsClasses.includes("pb-interactive") && !seriesClasses.includes("pb-interactive");
            }""",
            timeout=5000,
        )

        assert "/downloads" in authed_page.url
        downloads_classes = shell.link("downloads").get_attribute("class") or ""
        series_classes = shell.link("series").get_attribute("class") or ""

        assert "pb-interactive" in downloads_classes
        assert "pb-interactive" not in series_classes

    def test_boosted_sidebar_navigation_keeps_standard_browser_title_casing(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/settings")

        for link_key, expected_url, expected_title in (
            ("series", "/series", "Series — Pullbox"),
            ("pull-list", "/pull-list", "Pull List — Pullbox"),
            ("downloads", "/downloads", "Downloads — Pullbox"),
            ("system", "/system", "System — Pullbox"),
        ):
            shell.link(link_key).click()
            wait_for_htmx(authed_page)
            authed_page.wait_for_function(
                """
                ([path, title]) => (
                  window.location.pathname === path &&
                  document.title === title
                )
                """,
                arg=[expected_url, expected_title],
                timeout=5000,
            )

    def test_sidebar_navigation_replaces_stale_page_footer_dock(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")

        stale_footer_height = authed_page.locator("[data-testid='page-footer-dock']").evaluate(
            """
            (footer) => {
              footer.innerHTML = `
                <div data-testid="stale-footer-dock">
                  <div class="page-dock-inner" data-testid="page-dock-inner">
                    <div class="page-dock-pagination" data-testid="page-dock-pagination">
                      <nav><span>1</span><span>2</span></nav>
                    </div>
                  </div>
                </div>
              `;
              return footer.querySelector("[data-testid='page-dock-inner']")
                .getBoundingClientRect().height;
            }
            """
        )
        assert stale_footer_height >= 38

        shell.link("settings").click()
        wait_for_htmx(authed_page)
        authed_page.wait_for_function(
            """() => {
                const url = new URL(window.location.href);
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                const dock = document.querySelector("[data-testid='page-dock-inner']");
                return (
                    url.pathname === "/settings" &&
                    !!footer &&
                    !!dock &&
                    !!footer.querySelector("[data-testid='settings-footer-dock']") &&
                    !footer.querySelector("[data-testid='series-pagination']") &&
                    dock.getBoundingClientRect().height <= 34
                );
            }""",
            timeout=5000,
        )

        assert authed_page.locator("[data-testid='page-footer-dock']").count() == 1
        assert authed_page.locator("[data-testid='settings-footer-dock']").count() == 1

    def test_sidebar_series_link_restores_saved_series_url(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        shell = AppShellPage(authed_page, seeded_server)

        series.goto()
        series.search("Batman")
        series.set_filter("status", "continuing")

        shell.link("health").click()
        wait_for_htmx(authed_page)
        assert "/health" in authed_page.url

        shell.link("series").click()
        wait_for_htmx(authed_page)

        parsed = parse_qs(urlparse(authed_page.url).query)
        assert authed_page.url.endswith("/series?q=Batman&status=continuing") or (
            urlparse(authed_page.url).path == "/series"
            and parsed.get("q") == ["Batman"]
            and parsed.get("status") == ["continuing"]
        )
        assert series.selected_value("status") == "continuing"
        assert series.query_param("q") == "Batman"

    def test_sidebar_logo_navigation_keeps_shell_mounted(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/intervention")

        marker = authed_page.evaluate(
            """() => {
                var sidebar = document.querySelector("[data-testid='app-sidebar']");
                var interventionBadge = document.querySelector("[data-testid='sidebar-badge-intervention']");
                var healthBadge = document.querySelector("[data-testid='sidebar-badge-health']");

                sidebar.setAttribute("data-e2e-shell-marker", "sidebar-shell");
                if (interventionBadge) interventionBadge.setAttribute("data-e2e-badge-marker", "intervention");
                if (healthBadge) healthBadge.setAttribute("data-e2e-badge-marker", "health");

                return {
                    sidebar: sidebar.getAttribute("data-e2e-shell-marker"),
                    intervention: interventionBadge && interventionBadge.getAttribute("data-e2e-badge-marker"),
                    health: healthBadge && healthBadge.getAttribute("data-e2e-badge-marker"),
                };
            }"""
        )

        shell.logo_link.click()
        wait_for_htmx(authed_page)

        assert urlparse(authed_page.url).path == "/"
        assert (
            authed_page.evaluate(
                """() => ({
                sidebar: document.querySelector("[data-testid='app-sidebar']")?.getAttribute("data-e2e-shell-marker"),
                intervention: document.querySelector("[data-testid='sidebar-badge-intervention']")?.getAttribute("data-e2e-badge-marker"),
                health: document.querySelector("[data-testid='sidebar-badge-health']")?.getAttribute("data-e2e-badge-marker"),
            })"""
            )
            == marker
        )

        authed_page.wait_for_function(
            """() => {
                const el = document.querySelector("[data-testid='sidebar-link-dashboard']");
                return el && el.className.includes('pb-interactive');
            }""",
            timeout=5000,
        )
        intervention_classes = shell.link("intervention").get_attribute("class") or ""
        assert "pb-interactive" not in intervention_classes

    def test_sidebar_badges_are_preloaded_and_stable_before_first_poll(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        requests: list[str] = []

        def capture_sidebar_badge_request(request) -> None:  # type: ignore[no-untyped-def]
            if request.url.endswith("/htmx/intervention/count") or request.url.endswith(
                "/health/badge"
            ):
                requests.append(request.url)

        authed_page.on("request", capture_sidebar_badge_request)

        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/series")
        authed_page.wait_for_timeout(800)

        assert shell.badge("intervention").locator("span").first.is_visible()
        assert shell.badge("health").count() == 1
        assert requests == []

    def test_header_add_series_action_stays_visible_across_navigation(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/")

        assert shell.add_series_button.is_visible()
        assert authed_page.locator("[data-testid='header-add-series']").count() == 1

        shell.link("series").click()
        wait_for_htmx(authed_page)

        assert "/series" in authed_page.url
        assert shell.add_series_button.is_visible()
        assert authed_page.locator("[data-testid='header-add-series']").count() == 1

        shell.link("downloads").click()
        wait_for_htmx(authed_page)

        assert "/downloads" in authed_page.url
        assert shell.add_series_button.is_visible()
        assert authed_page.locator("[data-testid='header-add-series']").count() == 1

    @pytest.mark.parametrize(
        ("import_tab", "destination", "destination_path", "destination_selector"),
        [
            ("collection", "downloads", "/downloads", "[data-testid='downloads-page']"),
            ("history", "health", "/health", "[data-testid='health-page']"),
            ("unmatched", "intervention", "/intervention", "[data-testid='intervention-page']"),
        ],
    )
    def test_sidebar_navigation_from_import_tabs_updates_url_and_survives_refresh(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        import_tab: str,
        destination: str,
        destination_path: str,
        destination_selector: str,
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        shell = AppShellPage(authed_page, seeded_server)

        import_page.goto(tab=import_tab)
        if import_tab == "collection":
            import_page.collection_panel.wait_for(state="visible", timeout=5000)
        elif import_tab == "history":
            import_page.history_panel.wait_for(state="visible", timeout=5000)
        else:
            import_page.unmatched_panel.wait_for(state="visible", timeout=5000)

        shell.link(destination).click()
        wait_for_htmx(authed_page)
        authed_page.locator(destination_selector).first.wait_for(state="visible", timeout=5000)

        assert urlparse(authed_page.url).path == destination_path

        authed_page.reload(wait_until="networkidle")
        authed_page.locator(destination_selector).first.wait_for(state="visible", timeout=5000)

        assert urlparse(authed_page.url).path == destination_path

    def test_sidebar_navigation_does_not_emit_alpine_collapse_warnings(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        warnings: list[str] = []

        def capture_console(message) -> None:  # type: ignore[no-untyped-def]
            text = message.text
            if "x-collapse" in text or "Collapse plugin" in text:
                warnings.append(text)

        authed_page.on("console", capture_console)

        shell = AppShellPage(authed_page, seeded_server)
        shell.goto("/downloads")
        shell.link("series").click()
        wait_for_htmx(authed_page)
        shell.link("downloads").click()
        wait_for_htmx(authed_page)
        authed_page.wait_for_timeout(250)

        assert warnings == []
