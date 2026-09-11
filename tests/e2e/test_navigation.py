"""
Navigation and page load E2E tests.

Verifies all main pages load without errors, sidebar navigation works,
HTMX partials render correctly, and no JavaScript console errors occur.

Run:
    pytest tests/e2e/test_navigation.py -v --browser chromium
"""

from __future__ import annotations

import typing
from urllib.parse import parse_qs, urlparse

import pytest

from tests.e2e.conftest import wait_for_htmx

pytestmark = pytest.mark.e2e


def _query_param(url: str, name: str) -> str | None:
    values = parse_qs(urlparse(url).query).get(name)
    return values[0] if values else None


def _wait_for_animation_frames(page, count: int = 3) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """(count) => new Promise((resolve) => {
            function step(remaining) {
                if (remaining <= 0) {
                    resolve();
                    return;
                }
                requestAnimationFrame(() => step(remaining - 1));
            }
            step(count);
        })""",
        count,
    )


def _install_series_flash_monitor(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """() => {
            window.__pbSeriesFlashMonitor = (() => {
                let running = false;
                let rafId = 0;
                let observer = null;
                let stats = null;

                function resetStats() {
                    stats = {
                        samples: 0,
                        missingFrames: 0,
                        hiddenFrames: 0,
                        collapsedFrames: 0,
                        blankFrames: 0,
                        removedTargetEvents: 0,
                    };
                }

                function hasMeaningfulContent(el) {
                    return Boolean(
                        el.querySelector(
                            "#series-compact > *, #series-grid > *, [data-testid='series-mission-control-table'] tbody tr, #series-pagination, [data-series-empty-state='true']"
                        )
                    );
                }

                function sample() {
                    if (!running) {
                        return;
                    }

                    stats.samples += 1;
                    const el = document.getElementById("series-results");
                    if (!el) {
                        stats.missingFrames += 1;
                        rafId = requestAnimationFrame(sample);
                        return;
                    }

                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (
                        style.display === "none" ||
                        style.visibility === "hidden" ||
                        parseFloat(style.opacity || "1") < 0.05
                    ) {
                        stats.hiddenFrames += 1;
                    }
                    if (rect.width < 120 || rect.height < 120) {
                        stats.collapsedFrames += 1;
                    }
                    if (!hasMeaningfulContent(el)) {
                        stats.blankFrames += 1;
                    }

                    rafId = requestAnimationFrame(sample);
                }

                function start() {
                    resetStats();
                    running = true;
                    const host = document.getElementById("content") || document.body;
                    observer = new MutationObserver(() => {
                        if (!document.getElementById("series-results")) {
                            stats.removedTargetEvents += 1;
                        }
                    });
                    observer.observe(host, { childList: true, subtree: true });
                    rafId = requestAnimationFrame(sample);
                }

                function stop() {
                    running = false;
                    if (rafId) {
                        cancelAnimationFrame(rafId);
                    }
                    if (observer) {
                        observer.disconnect();
                    }
                    return { ...stats };
                }

                resetStats();
                return { start, stop };
            })();
        }"""
    )


def _assert_no_flash_stats(stats: dict[str, int]) -> None:
    assert stats["samples"] > 0
    assert stats["missingFrames"] == 0, stats
    assert stats["hiddenFrames"] == 0, stats
    assert stats["collapsedFrames"] == 0, stats
    assert stats["blankFrames"] == 0, stats
    assert stats["removedTargetEvents"] == 0, stats


def _install_shell_content_flash_monitor(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """() => {
            window.__pbShellFlashMonitor = (() => {
                let running = false;
                let rafId = 0;
                let stats = null;

                function resetStats() {
                    stats = {
                        samples: 0,
                        missingFrames: 0,
                        hiddenFrames: 0,
                        blankFrames: 0,
                    };
                }

                function hasMeaningfulContent(el) {
                    return Boolean(
                        el.querySelector(
                            "[data-testid='downloads-page'], [data-testid='post-processing-page']"
                        )
                    );
                }

                function sample() {
                    if (!running) {
                        return;
                    }

                    stats.samples += 1;
                    const el = document.getElementById("content");
                    if (!el) {
                        stats.missingFrames += 1;
                        rafId = requestAnimationFrame(sample);
                        return;
                    }

                    const style = window.getComputedStyle(el);
                    if (
                        style.display === "none" ||
                        style.visibility === "hidden" ||
                        parseFloat(style.opacity || "1") < 0.95
                    ) {
                        stats.hiddenFrames += 1;
                    }

                    if (!hasMeaningfulContent(el)) {
                        stats.blankFrames += 1;
                    }

                    rafId = requestAnimationFrame(sample);
                }

                function start() {
                    resetStats();
                    running = true;
                    rafId = requestAnimationFrame(sample);
                }

                function stop() {
                    running = false;
                    if (rafId) {
                        cancelAnimationFrame(rafId);
                    }
                    return { ...stats };
                }

                resetStats();
                return { start, stop };
            })();
        }"""
    )


def _assert_no_shell_flash_stats(stats: dict[str, int]) -> None:
    assert stats["samples"] > 0
    assert stats["missingFrames"] == 0, stats
    assert stats["hiddenFrames"] == 0, stats
    assert stats["blankFrames"] == 0, stats


def _footer_clearance_metrics(page) -> dict[str, float | str | None]:  # type: ignore[no-untyped-def]
    return typing.cast(
        "dict[str, float | str | None]",
        page.evaluate(
            """() => {
                const content = document.querySelector("#content");
                const dock = document.querySelector("#page-footer-dock");
                const spacer = document.querySelector("[data-testid='page-footer-clearance']");
                if (!content || !dock || !spacer) {
                    return null;
                }

                content.scrollTop = content.scrollHeight;
                content.dispatchEvent(new Event("scroll"));

                const contentChildren = Array.from(content.children)
                    .filter((el) => el !== spacer && el.tagName !== "SCRIPT");
                const lastContent = contentChildren[contentChildren.length - 1] || null;
                const spacerBox = spacer.getBoundingClientRect();
                const dockBox = dock.getBoundingClientRect();
                const lastBox = lastContent ? lastContent.getBoundingClientRect() : null;
                const spacerStyle = window.getComputedStyle(spacer);
                const visualContentSelector = [
                    "tbody tr",
                    "[data-testid='series-grid-card']",
                    "[data-testid='series-compact-card']",
                    "[data-testid='whats-new-release-row']",
                    "[data-testid='whats-new-compact-release-row']",
                    ".series-mission-control-table-wrap",
                    ".downloads-table-wrap",
                ].join(",");
                const visualContent = Array.from(content.querySelectorAll(visualContentSelector))
                    .filter((el) => {
                        const style = window.getComputedStyle(el);
                        if (style.display === "none" || style.visibility === "hidden") {
                            return false;
                        }
                        const box = el.getBoundingClientRect();
                        return box.width > 0 && box.height > 1 && box.bottom <= dockBox.top + 2;
                    });
                const visibleDescendantBottom = visualContent.reduce((bottom, el) => {
                        return Math.max(bottom, el.getBoundingClientRect().bottom);
                    }, 0);

                return {
                    spacerHeight: spacerBox.height,
                    spacerDisplay: spacerStyle.display,
                    spacerTop: spacerBox.top,
                    spacerBottom: spacerBox.bottom,
                    dockTop: dockBox.top,
                    lastContentBottom: lastBox ? lastBox.bottom : null,
                    contentToDockGap: lastBox ? dockBox.top - lastBox.bottom : null,
                    visibleContentToDockGap: visibleDescendantBottom > 0
                        ? dockBox.top - visibleDescendantBottom
                        : null,
                    spacerToDockGap: dockBox.top - spacerBox.bottom,
                };
            }"""
        ),
    )


def _select_filter_value(page, name: str, value: str) -> None:  # type: ignore[no-untyped-def]
    testid = f"series-{name.replace('_', '-')}-select"
    root = page.locator(f"[data-testid='{testid}']").first
    root.locator("[data-dropdown-select-trigger]").first.click()
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    panel = page.locator("[data-dropdown-select-panel]:visible").first
    panel.wait_for(state="visible", timeout=5000)
    panel.locator(f'[data-dropdown-option][data-value="{escaped_value}"]').first.click()


def _assert_select_state(
    page,
    name: str,
    expected_value: str,
    expected_label: str,
) -> None:  # type: ignore[no-untyped-def]
    testid = f"series-{name.replace('_', '-')}-select"
    control = page.locator(f"[data-testid='{testid}']").first
    actual_value = control.locator("[data-dropdown-select-input]").first.input_value()
    assert actual_value == expected_value
    actual_label = (
        control.locator("[data-dropdown-select-trigger-label]").first.text_content() or ""
    ).strip()
    assert actual_label == expected_label


class TestPageLoads:
    """Verify each main page loads without errors (no 500, no JS errors)."""

    @pytest.fixture(autouse=True)
    def _capture_console_errors(self, authed_page) -> None:  # type: ignore[no-untyped-def]
        """Capture JS console errors for all tests in this class."""
        self.console_errors: list[str] = []
        authed_page.on(
            "console",
            lambda msg: self.console_errors.append(msg.text) if msg.type == "error" else None,
        )

    def _assert_no_js_errors(self) -> None:
        """Fail if any JS console errors were captured."""
        # Filter out known benign errors
        real_errors = [
            e for e in self.console_errors if "favicon" not in e.lower() and "404" not in e
        ]
        assert real_errors == [], f"JS console errors: {real_errors}"

    def test_dashboard_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/")
        assert authed_page.locator("aside").count() > 0
        self._assert_no_js_errors()

    def test_series_list_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/series")
        assert "Series" in authed_page.title() or "series" in authed_page.url
        self._assert_no_js_errors()

    def test_downloads_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/downloads")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_library_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/library")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_blocklist_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/blocklist")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_intervention_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/intervention")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_import_collection_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/import?tab=collection")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_import_history_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/import?tab=history")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_import_orphaned_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/import?tab=follow-up")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_search_history_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/search-history")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_post_processing_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/post-processing")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_settings_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/settings")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_health_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/health")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_security_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/security")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_utilities_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/utilities")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_utilities_converter_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/utilities/converter")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()

    def test_system_page_loads(self, authed_page, seeded_server: str) -> None:  # type: ignore[no-untyped-def]
        authed_page.goto(f"{seeded_server}/system")
        assert "/login" not in authed_page.url
        self._assert_no_js_errors()


class TestSidebarNavigation:
    """Verify sidebar links navigate correctly."""

    SIDEBAR_LINKS: typing.ClassVar[list[tuple[str, str]]] = [
        ("/", "Dashboard"),
        ("/series", "Series"),
        ("/library", "Library"),
        ("/downloads", "Downloads"),
        ("/settings", "Settings"),
        ("/health", "Health"),
        ("/security", "Security"),
    ]

    def test_sidebar_links_navigate_correctly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Click each sidebar nav link and verify URL changes."""
        authed_page.goto(f"{seeded_server}/")

        for path, _label in self.SIDEBAR_LINKS:
            link = authed_page.locator(f"aside a[href='{path}']").first
            if link.count() > 0 and link.is_visible():
                link.click()
                authed_page.wait_for_load_state("networkidle", timeout=5000)
                assert "/login" not in authed_page.url

    def test_sidebar_highlights_active_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Active page's sidebar link has a visual indicator (blue text)."""
        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        # The active link should have the interactive highlight class (design system token)
        active_link = authed_page.locator("aside a[href='/series']").first
        if active_link.count() > 0:
            classes = active_link.get_attribute("class") or ""
            assert "pb-interactive" in classes, f"Expected interactive highlight, got: {classes}"

    def test_sidebar_collapse_toggle(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Sidebar collapse button toggles sidebar width."""
        authed_page.goto(f"{seeded_server}/")

        # Find the collapse button (desktop only, hidden on mobile)
        collapse_btn = authed_page.locator("[data-testid='sidebar-collapse-toggle']").first
        if collapse_btn.count() > 0 and collapse_btn.is_visible():
            collapse_btn.click()
            authed_page.wait_for_function(
                """() => {
                    const button = document.querySelector("[data-testid='sidebar-collapse-toggle']");
                    return button?.getAttribute("data-tip") === "Expand sidebar";
                }""",
                timeout=5000,
            )
            # Click again to expand
            collapse_btn.click()
            authed_page.wait_for_function(
                """() => {
                    const button = document.querySelector("[data-testid='sidebar-collapse-toggle']");
                    return button?.getAttribute("data-tip") === "Collapse sidebar";
                }""",
                timeout=5000,
            )

    def test_sidebar_navigation_between_downloads_and_post_processing_does_not_flash(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.goto(f"{seeded_server}/downloads")
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        _install_shell_content_flash_monitor(authed_page)

        authed_page.evaluate("() => window.__pbShellFlashMonitor.start()")
        authed_page.locator("aside a[href='/post-processing']").first.click()
        authed_page.wait_for_url("**/post-processing**", timeout=5000)
        authed_page.locator("[data-testid='post-processing-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )
        _wait_for_animation_frames(authed_page)
        to_post_processing_stats = authed_page.evaluate("() => window.__pbShellFlashMonitor.stop()")
        _assert_no_shell_flash_stats(to_post_processing_stats)

        authed_page.evaluate("() => window.__pbShellFlashMonitor.start()")
        authed_page.locator("aside a[href='/downloads']").first.click()
        authed_page.wait_for_url("**/downloads**", timeout=5000)
        authed_page.locator("[data-testid='downloads-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )
        _wait_for_animation_frames(authed_page)
        to_downloads_stats = authed_page.evaluate("() => window.__pbShellFlashMonitor.stop()")
        _assert_no_shell_flash_stats(to_downloads_stats)


class TestShellLayoutContract:
    """Verify shared app-shell spacing contracts across page families."""

    def test_semantic_interactive_controls_use_shared_cursor_affordances(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.goto(f"{seeded_server}/series")
        authed_page.locator("[data-testid='series-page']").wait_for(
            state="visible",
            timeout=5000,
        )

        cursors = authed_page.evaluate(
            """() => {
                const fixture = document.createElement("div");
                fixture.innerHTML = `
                    <a data-cursor-test="link" href="#cursor-test">Link</a>
                    <button data-cursor-test="button" type="button">Button</button>
                    <div data-cursor-test="role-button" role="button" tabindex="0">Action</div>
                    <button data-cursor-test="disabled" type="button" disabled>Disabled</button>
                `;
                document.body.appendChild(fixture);
                return Object.fromEntries(
                    [...fixture.querySelectorAll("[data-cursor-test]")].map((node) => [
                        node.dataset.cursorTest,
                        getComputedStyle(node).cursor,
                    ]),
                );
            }"""
        )

        assert cursors == {
            "link": "pointer",
            "button": "pointer",
            "role-button": "pointer",
            "disabled": "not-allowed",
        }

    @pytest.mark.parametrize(
        ("path", "ready_selector"),
        [
            ("/settings", "[data-testid='settings-page']"),
            ("/series?per_page=50", "[data-testid='series-page']"),
            ("/whats-new?per_page=25", "[data-testid='whats-new-page']"),
            ("/downloads", "[data-testid='downloads-page']"),
            ("/blocklist", "[data-testid='blocklist-page']"),
        ],
    )
    def test_pages_keep_footer_clearance_above_dock(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        path: str,
        ready_selector: str,
    ) -> None:
        authed_page.set_viewport_size({"width": 1440, "height": 640})
        authed_page.goto(f"{seeded_server}{path}")
        authed_page.locator(ready_selector).first.wait_for(state="visible", timeout=5000)
        wait_for_htmx(authed_page)
        _wait_for_animation_frames(authed_page)

        metrics = _footer_clearance_metrics(authed_page)

        assert metrics is not None
        assert metrics["spacerDisplay"] != "none"
        assert metrics["spacerHeight"] is not None
        assert metrics["spacerHeight"] >= 20
        assert metrics["contentToDockGap"] is not None
        assert metrics["contentToDockGap"] >= 20
        if metrics["visibleContentToDockGap"] is not None:
            assert metrics["visibleContentToDockGap"] >= 20
        assert metrics["spacerToDockGap"] is not None
        assert metrics["spacerToDockGap"] >= -2


class TestHTMXBehavior:
    """Verify HTMX partial swaps work without full page reloads."""

    def test_series_search_filters_without_reload(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Typing in series search triggers HTMX partial swap, not full reload."""
        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        search = authed_page.locator("[data-testid='series-search-input']").first
        if search.count() > 0 and search.is_visible():
            search.fill("nonexistent-series-query")
            authed_page.wait_for_function(
                "() => new URL(window.location.href).searchParams.get('q') === 'nonexistent-series-query'",
                timeout=5000,
            )
            wait_for_htmx(authed_page)
            # Page should still be on /series (no full navigation)
            assert "/series" in authed_page.url

    def test_settings_tab_switching(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Clicking settings tabs loads content without full reload."""
        authed_page.goto(f"{seeded_server}/settings")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        # Look for tab-like elements on the settings page
        tabs = authed_page.locator("a[href*='/settings'], button[hx-get*='/settings']")
        if tabs.count() > 1:
            # Click the second tab
            target_tab = tabs.nth(1)
            target_href = target_tab.get_attribute("href") or ""
            target_tab.click()
            if target_href:
                authed_page.wait_for_function(
                    """(href) => {
                        const expected = new URL(href, window.location.origin);
                        const current = new URL(window.location.href);
                        return (
                            current.pathname === expected.pathname &&
                            current.search === expected.search
                        );
                    }""",
                    arg=target_href,
                    timeout=5000,
                )
            # Should still be on settings (partial swap, not navigation)
            assert "/settings" in authed_page.url

    def test_series_pagination_preserves_shell_layout(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Paginating /series swaps only the results region and keeps the shell pinned."""
        authed_page.goto(f"{seeded_server}/series?per_page=2")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        header_box = authed_page.locator("header").first.bounding_box()
        registry_box = authed_page.locator(
            "[data-testid='series-registry-header']"
        ).first.bounding_box()
        filter_box = authed_page.locator("#series-filter-form").first.bounding_box()
        assert header_box is not None
        assert registry_box is not None
        assert filter_box is not None
        assert "filter-bar-hidden" not in (
            authed_page.locator("#series-filter-form").first.get_attribute("class") or ""
        )
        assert registry_box["y"] >= header_box["y"] + header_box["height"] - 2
        assert filter_box["y"] >= registry_box["y"] + registry_box["height"] - 2

        shell_tokens = authed_page.evaluate(
            """() => {
                const header = document.querySelector("#main-area header");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                if (!header || !footer) return null;

                header.dataset.e2eToken = header.dataset.e2eToken || Math.random().toString(36).slice(2);
                footer.dataset.e2eToken = footer.dataset.e2eToken || Math.random().toString(36).slice(2);

                return {
                    header: header.dataset.e2eToken,
                    footer: footer.dataset.e2eToken,
                };
            }"""
        )
        assert shell_tokens is not None

        search = authed_page.locator("[data-testid='series-search-input']").first
        pagination = authed_page.locator("#series-pagination").first
        footer = authed_page.locator("[data-testid='page-footer-dock']").first

        assert search.is_visible()
        assert pagination.is_visible()
        assert footer.is_visible()

        authed_page.locator("[data-testid='series-pagination-next']").first.click()
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('page') === '2'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        assert _query_param(authed_page.url, "page") == "2"

        tokens_after = authed_page.evaluate(
            """() => {
                const header = document.querySelector("#main-area header");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                if (!header || !footer) return null;

                return {
                    header: header.dataset.e2eToken || null,
                    footer: footer.dataset.e2eToken || null,
                };
            }"""
        )
        assert tokens_after == shell_tokens
        assert search.is_visible()
        assert pagination.is_visible()
        assert footer.is_visible()
        assert "filter-bar-hidden" not in (
            authed_page.locator("#series-filter-form").first.get_attribute("class") or ""
        )

        authed_page.locator("[data-testid='series-pagination-prev']").first.click()
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('page') === '1'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        assert _query_param(authed_page.url, "page") == "1"

    def test_series_results_do_not_blank_during_htmx_updates(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Pagination, filters, and sorting should not blank the results region between frames."""
        authed_page.goto(f"{seeded_server}/series?per_page=2")
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        _install_series_flash_monitor(authed_page)

        authed_page.evaluate("() => window.__pbSeriesFlashMonitor.start()")
        authed_page.locator("[data-testid='series-pagination-next']").first.click()
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('page') === '2'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        _wait_for_animation_frames(authed_page)
        pagination_stats = authed_page.evaluate("() => window.__pbSeriesFlashMonitor.stop()")
        _assert_no_flash_stats(pagination_stats)

        authed_page.evaluate("() => window.__pbSeriesFlashMonitor.start()")
        _select_filter_value(authed_page, "status", "continuing")
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('status') === 'continuing'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        _wait_for_animation_frames(authed_page)
        status_stats = authed_page.evaluate("() => window.__pbSeriesFlashMonitor.stop()")
        _assert_no_flash_stats(status_stats)

        authed_page.evaluate("() => window.__pbSeriesFlashMonitor.start()")
        _select_filter_value(authed_page, "sort", "-title")
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('sort') === '-title'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        _wait_for_animation_frames(authed_page)
        sort_stats = authed_page.evaluate("() => window.__pbSeriesFlashMonitor.stop()")
        _assert_no_flash_stats(sort_stats)

        authed_page.evaluate("() => window.__pbSeriesFlashMonitor.start()")
        _select_filter_value(authed_page, "per_page", "50")
        authed_page.wait_for_function(
            "() => new URL(window.location.href).searchParams.get('per_page') === '50'",
            timeout=5000,
        )
        wait_for_htmx(authed_page)
        _wait_for_animation_frames(authed_page)
        per_page_stats = authed_page.evaluate("() => window.__pbSeriesFlashMonitor.stop()")
        _assert_no_flash_stats(per_page_stats)

    def test_series_detail_back_link_restores_last_list_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Detail-page back navigation should restore the last canonical /series URL."""
        authed_page.goto(f"{seeded_server}/series?status=continuing&per_page=2&page=2")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        first_series_link = authed_page.locator("[data-testid='series-item-link']:visible").first
        assert first_series_link.is_visible()
        first_series_link.click()
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        back_link = authed_page.locator("a[data-series-index-link='true']").first
        assert back_link.is_visible()
        assert "status=continuing" in (back_link.get_attribute("href") or "")
        assert "per_page=2" in (back_link.get_attribute("href") or "")
        assert "page=2" in (back_link.get_attribute("href") or "")

        back_link.click()
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        authed_page.locator("[data-testid='series-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        assert "/series" in authed_page.url
        assert _query_param(authed_page.url, "status") == "continuing"
        assert _query_param(authed_page.url, "per_page") == "2"
        assert _query_param(authed_page.url, "page") == "2"
        _assert_select_state(authed_page, "status", "continuing", "Continuing")
        _assert_select_state(authed_page, "per_page", "2", "2")

    def test_browser_back_from_series_detail_restores_list_without_cover_modal_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Browser back from Series Detail should not leave orphaned cover modal bindings behind."""
        console_errors: list[str] = []
        page_errors: list[str] = []
        authed_page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        authed_page.on("pageerror", lambda error: page_errors.append(str(error)))

        authed_page.goto(f"{seeded_server}/series?status=continuing&per_page=2&page=2")
        authed_page.wait_for_load_state("networkidle", timeout=5000)

        first_series_link = authed_page.locator("[data-testid='series-item-link']:visible").first
        expected_series_href = first_series_link.get_attribute("href")
        assert expected_series_href

        first_series_link.click()
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible", timeout=5000
        )

        console_errors.clear()
        page_errors.clear()

        authed_page.go_back()
        authed_page.wait_for_load_state("networkidle", timeout=5000)
        authed_page.locator("[data-testid='series-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        assert "/series" in authed_page.url
        assert _query_param(authed_page.url, "status") == "continuing"
        assert _query_param(authed_page.url, "per_page") == "2"
        assert _query_param(authed_page.url, "page") == "2"
        _assert_select_state(authed_page, "status", "continuing", "Continuing")
        _assert_select_state(authed_page, "per_page", "2", "2")

        real_console_errors = [
            error
            for error in console_errors
            if "favicon" not in error.lower() and "404" not in error
        ]
        assert real_console_errors == []
        assert page_errors == []

    def test_browser_back_and_forward_restore_series_routes_cleanly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Browser history should restore Series Detail and Series List without stale detail UI state."""
        console_errors: list[str] = []
        page_errors: list[str] = []
        forward_requests: list[str] = []
        authed_page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        authed_page.on("pageerror", lambda error: page_errors.append(str(error)))
        authed_page.on("request", lambda request: forward_requests.append(request.url))

        authed_page.goto(f"{seeded_server}/series?q=Batman&sort=title&per_page=25")

        first_series_link = (
            authed_page.locator("[data-testid='series-item-link']").filter(has_text="Batman").first
        )
        first_series_link.wait_for(state="visible", timeout=5000)
        expected_series_href = first_series_link.get_attribute("href")
        assert expected_series_href

        first_series_link.click()
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible", timeout=5000
        )

        issues_status = authed_page.locator(
            "[data-testid='series-detail-issues-status-select']"
        ).first
        issues_status.locator("[data-dropdown-select-trigger]").first.click()
        authed_page.locator(
            "[data-dropdown-select-panel]:visible [data-dropdown-option][data-value='wanted']"
        ).first.click()
        wait_for_htmx(authed_page)
        authed_page.wait_for_function(
            "() => document.querySelector(\"[data-testid='series-detail-issues-status-select'] [data-dropdown-select-trigger-label]\")?.textContent?.trim() === 'Wanted'",
            timeout=5000,
        )

        first_issue_link = authed_page.locator("[data-testid='series-issue-link']").first
        expected_issue_href = first_issue_link.get_attribute("href")
        assert expected_issue_href

        first_issue_link.click()
        authed_page.wait_for_url("**/issues/**", timeout=5000)
        authed_page.locator("[data-testid='issue-detail-page']").first.wait_for(
            state="visible", timeout=5000
        )
        assert expected_issue_href in authed_page.url

        console_errors.clear()
        page_errors.clear()

        authed_page.go_back()
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible", timeout=5000
        )
        assert authed_page.locator("[data-testid='issue-detail-page']").count() == 0

        authed_page.go_back()
        authed_page.wait_for_function(
            """() => {
                const url = new URL(window.location.href);
                return (
                    url.pathname === "/series"
                    && url.searchParams.get("q") === "Batman"
                    && url.searchParams.get("sort") === "title"
                    && url.searchParams.get("per_page") === "25"
                );
            }""",
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )
        assert (
            authed_page.locator("[data-testid='series-search-input']").first.input_value()
            == "Batman"
        )
        _assert_select_state(authed_page, "sort", "title", "Title A–Z")
        _assert_select_state(authed_page, "per_page", "25", "25")

        console_errors.clear()
        page_errors.clear()
        forward_requests.clear()

        authed_page.go_forward()
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        issues_status = authed_page.locator(
            "[data-testid='series-detail-issues-status-select']"
        ).first
        issues_status.wait_for(state="visible", timeout=5000)
        assert (
            issues_status.locator("[data-dropdown-select-trigger-label]").first.text_content() or ""
        ).strip() == "All Status"
        assert authed_page.locator("[data-dropdown-select-panel]:visible").count() == 0
        assert authed_page.locator("[data-testid='issue-detail-page']").count() == 0
        unwanted_issue_requests = [
            url
            for url in forward_requests
            if "/htmx/series/" in url and "issue_status=wanted" in url
        ]
        assert unwanted_issue_requests == []

        real_console_errors = [
            error
            for error in console_errors
            if "favicon" not in error.lower() and "404" not in error
        ]
        assert real_console_errors == []
        assert page_errors == []

    def test_browser_forward_to_series_detail_from_list_does_not_restore_stale_issue_filter(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Forward navigation back to Series Detail should not revive stale wanted-filter state."""
        console_errors: list[str] = []
        page_errors: list[str] = []
        forward_requests: list[str] = []
        authed_page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        authed_page.on("pageerror", lambda error: page_errors.append(str(error)))
        authed_page.on("request", lambda request: forward_requests.append(request.url))

        authed_page.goto(f"{seeded_server}/series?q=Saga&sort=title&per_page=25")
        authed_page.locator("[data-testid='series-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        first_series_link = (
            authed_page.locator("[data-testid='series-item-link']").filter(has_text="Saga").first
        )
        first_series_link.wait_for(state="visible", timeout=5000)
        expected_series_href = first_series_link.get_attribute("href")
        assert expected_series_href

        first_series_link.click()
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible", timeout=5000
        )

        issues_status = authed_page.locator(
            "[data-testid='series-detail-issues-status-select']"
        ).first
        issues_status.locator("[data-dropdown-select-trigger]").first.click()
        authed_page.locator(
            "[data-dropdown-select-panel]:visible [data-dropdown-option][data-value='wanted']"
        ).first.click()
        wait_for_htmx(authed_page)
        authed_page.wait_for_function(
            "() => document.querySelector(\"[data-testid='series-detail-issues-status-select'] [data-dropdown-select-trigger-label]\")?.textContent?.trim() === 'Wanted'",
            timeout=5000,
        )

        authed_page.go_back()
        authed_page.wait_for_function(
            """() => {
                const url = new URL(window.location.href);
                return (
                    url.pathname === "/series"
                    && url.searchParams.get("q") === "Saga"
                    && url.searchParams.get("sort") === "title"
                    && url.searchParams.get("per_page") === "25"
                );
            }""",
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        console_errors.clear()
        page_errors.clear()
        forward_requests.clear()

        authed_page.go_forward()
        authed_page.wait_for_function(
            "(href) => window.location.pathname === href",
            arg=expected_series_href,
            timeout=5000,
        )
        authed_page.locator("[data-testid='series-detail-page']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        issues_status = authed_page.locator(
            "[data-testid='series-detail-issues-status-select']"
        ).first
        issues_status.wait_for(state="visible", timeout=5000)
        assert (
            issues_status.locator("[data-dropdown-select-trigger-label]").first.text_content() or ""
        ).strip() == "All Status"
        assert authed_page.locator("[data-dropdown-select-panel]:visible").count() == 0
        unwanted_issue_requests = [
            url
            for url in forward_requests
            if "/htmx/series/" in url and "issue_status=wanted" in url
        ]
        assert unwanted_issue_requests == []

        real_console_errors = [
            error
            for error in console_errors
            if "favicon" not in error.lower() and "404" not in error
        ]
        assert real_console_errors == []
        assert page_errors == []
