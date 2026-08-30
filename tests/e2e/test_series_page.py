"""Focused E2E coverage for the rewritten /series page."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

from tests.e2e.conftest import _run_async_blocking, wait_for_htmx
from tests.e2e.pages.series_list import SeriesListPage

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


async def _set_series_catalog_state(title: str, state: str) -> None:
    from sqlalchemy import select

    from pullbox.database import get_session_factory
    from pullbox.models.series import IssueCatalogState, Series

    factory = get_session_factory()
    async with factory() as session:
        series = (await session.execute(select(Series).where(Series.title == title))).scalar_one()
        series.issue_catalog_state = IssueCatalogState(state)
        await session.commit()


def _trigger_series_catalog_refresh(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """() => new Promise((resolve, reject) => {
            const target = document.querySelector("#series-results-body");
            if (!target || !window.htmx) {
                reject(new Error("Series results or HTMX was unavailable"));
                return;
            }

            target.__pbSelectionRefreshSentinel = true;
            const timeout = window.setTimeout(() => {
                reject(new Error("Series catalog refresh did not settle"));
            }, 5000);

            window.htmx.ajax("GET", target.getAttribute("hx-get"), {
                source: target,
                target,
                select: "#series-results-body",
                swap: target.getAttribute("hx-swap"),
            }).then(() => {
                window.clearTimeout(timeout);
                resolve();
            }).catch((error) => {
                window.clearTimeout(timeout);
                reject(error);
            });
        })"""
    )


def _wait_for_first_grid_cover_loaded(page) -> None:  # type: ignore[no-untyped-def]
    page.wait_for_function(
        """() => {
            const img = document.querySelector("[data-testid='series-grid-cover']");
            return Boolean(
                img
                && img.complete
                && img.naturalWidth > 0
                && window.getComputedStyle(img).display !== "none"
            );
        }""",
        timeout=5000,
    )


def _install_series_blank_monitor(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        """() => {
            window.__pbSeriesBlankMonitor = (() => {
                let running = false;
                let rafId = 0;
                let stats = null;

                function resetStats() {
                    stats = {
                        samples: 0,
                        missingBodyFrames: 0,
                        hiddenBodyFrames: 0,
                        collapsedBodyFrames: 0,
                        blankBodyFrames: 0,
                    };
                }

                function hasMeaningfulContent(el) {
                    return Boolean(
                        el.querySelector(
                            "[data-testid='series-result-card'], [data-testid='series-result-row'], [data-testid='series-compact-card'], [data-testid='series-grid-card'], [data-testid='series-empty-state']"
                        )
                    );
                }

                function sample() {
                    if (!running) {
                        return;
                    }

                    stats.samples += 1;
                    const el = document.querySelector("[data-testid='series-results-body']");
                    if (!el) {
                        stats.missingBodyFrames += 1;
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
                        stats.hiddenBodyFrames += 1;
                    }
                    if (rect.width < 120 || rect.height < 120) {
                        stats.collapsedBodyFrames += 1;
                    }
                    if (!hasMeaningfulContent(el)) {
                        stats.blankBodyFrames += 1;
                    }

                    rafId = requestAnimationFrame(sample);
                }

                return {
                    start() {
                        resetStats();
                        running = true;
                        rafId = requestAnimationFrame(sample);
                    },
                    stop() {
                        running = false;
                        if (rafId) {
                            cancelAnimationFrame(rafId);
                        }
                        return { ...stats };
                    },
                };
            })();
        }"""
    )


def _assert_no_blank_stats(stats: dict[str, int]) -> None:
    assert stats["samples"] > 0
    assert stats["missingBodyFrames"] == 0, stats
    assert stats["hiddenBodyFrames"] == 0, stats
    assert stats["collapsedBodyFrames"] == 0, stats
    assert stats["blankBodyFrames"] == 0, stats


def _grid_cover_visibility_stats(page):  # type: ignore[no-untyped-def]
    return page.evaluate(
        """() => {
            const cards = Array.from(document.querySelectorAll("[data-testid='series-grid-card']"));
            const coverCards = cards.filter((card) => card.querySelector("[data-testid='series-grid-cover']"));
            return {
                total: coverCards.length,
                loaded: coverCards.filter((card) => {
                    const img = card.querySelector("[data-testid='series-grid-cover']");
                    return img && img.naturalWidth > 0 && window.getComputedStyle(img).display !== "none";
                }).length,
            };
        }"""
    )


class TestSeriesPage:
    """Behavior-first E2E coverage for /series."""

    def test_view_toggle_tooltip_renders_on_hover(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        target = authed_page.locator("[data-testid='series-view-list']").first
        target.hover()

        assert target.get_attribute("data-tip") == "List view"

    def test_theme_toggle_tooltip_renders_on_hover(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        target = authed_page.locator("[data-testid='header-theme-toggle']").first
        target.hover()

        theme_tip = target.get_attribute("data-tip")
        assert theme_tip in {"Switch to light mode", "Switch to dark mode"}

    def test_sys_indicator_renders_a_visible_status_dot(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        styles = authed_page.evaluate(
            """() => {
                const dot = document.querySelector("[data-testid='series-monitored-indicator']");
                if (!dot) {
                    return null;
                }
                const style = window.getComputedStyle(dot);
                return {
                    width: style.width,
                    height: style.height,
                    backgroundColor: style.backgroundColor,
                    opacity: style.opacity,
                };
            }"""
        )

        assert styles is not None
        assert styles["width"] != "0px"
        assert styles["height"] != "0px"
        assert styles["backgroundColor"] not in ("rgba(0, 0, 0, 0)", "transparent")

    def test_initial_load_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        header_box = authed_page.locator("[data-testid='app-header']").bounding_box()
        toolbar_box = series.toolbar.bounding_box()

        assert header_box is not None
        assert toolbar_box is not None
        registry_header = authed_page.locator("[data-testid='series-registry-header']").first
        registry_box = registry_header.bounding_box()
        assert registry_box is not None
        assert registry_header.is_visible()
        assert authed_page.locator("[data-testid='series-registry-title']").first.is_visible()
        assert authed_page.locator("[data-testid='series-registry-gauges']").first.is_visible()
        assert authed_page.locator(
            "[data-testid='series-registry-gauge-overall']"
        ).first.is_visible()
        assert authed_page.locator(
            "[data-testid='series-registry-gauge-wanted']"
        ).first.is_visible()
        assert authed_page.locator(
            "[data-testid='series-registry-gauge-active-downloads']"
        ).first.is_visible()
        assert registry_box["y"] >= header_box["y"] + header_box["height"] + 12
        assert toolbar_box["y"] >= registry_box["y"] + registry_box["height"] - 2
        assert series.footer.is_visible()
        assert not series.summary.is_visible()
        assert authed_page.locator("[data-testid='series-mission-control-footer']").count() == 0
        add_series_box = authed_page.locator("[data-testid='header-add-series']").bounding_box()
        select_toggle_box = series.select_mode_toggle.bounding_box()
        assert add_series_box is not None
        assert select_toggle_box is not None
        assert abs(add_series_box["height"] - select_toggle_box["height"]) <= 8
        assert series.results_body.is_visible()
        assert series.select_mode_toggle.is_visible()
        assert not series.select_mode_toolbar.is_visible()
        assert authed_page.locator("[data-testid='series-mission-control-view']").first.is_visible()
        assert authed_page.locator("[data-testid='series-collector-wall-view']").count() == 0
        assert authed_page.locator("[data-testid='series-view-list']").first.is_visible()
        assert authed_page.locator("[data-testid='series-view-grid']").first.is_visible()
        assert series.current_view() == "list"
        assert series.visible_series_links().count() > 0
        assert series.selected_count_text() == "0 selected"
        view_toggle_box = authed_page.locator(
            "[data-testid='series-view-toggle']"
        ).first.bounding_box()
        assert view_toggle_box is not None
        assert abs(view_toggle_box["height"] - select_toggle_box["height"]) <= 2
        series.open_select_mode()
        select_visible_box = series.select_visible_button.bounding_box()
        assert select_visible_box is not None
        assert abs(select_visible_box["height"] - select_toggle_box["height"]) <= 2

    def test_select_toolbar_stacks_count_above_left_aligned_selection_actions(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        browse_toolbar = authed_page.locator("[data-testid='series-toolbar-frame']").first
        browse_box = browse_toolbar.bounding_box()
        assert browse_box is not None

        series.open_select_mode()

        assert authed_page.locator("[data-testid='series-selection-inline']").first.is_visible()
        assert "Selection" not in (
            authed_page.locator("[data-testid='series-selection-inline']").first.text_content()
            or ""
        )
        assert "Bulk actions" not in (series.select_mode_toolbar.text_content() or "")
        assert "Select visible results or the full filtered set first" not in (
            series.select_mode_toolbar.text_content() or ""
        )
        layout = authed_page.evaluate(
            """() => {
                const count = document.querySelector("[data-testid='series-selection-inline']");
                const row = document.querySelector("[data-testid='series-selection-controls-row']");
                const leftGroup = document.querySelector("[data-testid='series-select-visible']")?.closest(".series-selection-bulk");
                if (!count || !row || !leftGroup) {
                    return null;
                }
                const countRect = count.getBoundingClientRect();
                const rowRect = row.getBoundingClientRect();
                const leftRect = leftGroup.getBoundingClientRect();
                return {
                    countAboveRow: countRect.bottom <= rowRect.top,
                    leftAligned: Math.abs(leftRect.left - rowRect.left) <= 2,
                };
            }"""
        )
        assert layout is not None
        assert layout["countAboveRow"] is True
        assert layout["leftAligned"] is True

    def test_boosted_title_sync_decodes_html_entities(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        result = authed_page.evaluate(
            """() => {
                document.dispatchEvent(new CustomEvent("htmx:afterSwap", {
                    detail: {
                        target: document.getElementById("content"),
                        xhr: {
                            responseText: "<!doctype html><html><head><title>About Betty&#39;s Boob — Pullbox</title></head><body></body></html>"
                        }
                    }
                }));

                return {
                    hasHeaderTitle: Boolean(document.querySelector("#main-area header h1")),
                    title: document.title,
                };
            }"""
        )

        assert result["hasHeaderTitle"] is False
        assert result["title"] == "About Betty's Boob — Pullbox"

    def test_legacy_compact_preference_falls_back_to_list_view(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.add_init_script(
            "() => { localStorage.setItem('series_view', 'compact'); document.cookie = 'series_view=compact; path=/'; }"
        )
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")
        assert series.current_view() == "list"
        assert authed_page.locator("[data-testid='series-mission-control-view']").first.is_visible()
        assert authed_page.locator("[data-testid='series-collector-wall-view']").count() == 0
        assert series.visible_series_links().count() > 0

    def test_grid_view_renders_as_single_active_server_view(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2", preferred_view="grid")

        assert series.current_view() == "grid"
        assert authed_page.locator("[data-testid='series-collector-wall-view']").first.is_visible()
        assert authed_page.locator("[data-testid='series-mission-control-view']").count() == 0
        assert authed_page.locator("[data-testid='series-grid-card']").count() > 0
        assert authed_page.locator("[data-testid='series-grid-cover']").count() > 0
        assert authed_page.locator("[data-testid='series-grid-cover-placeholder']").count() > 0
        first_card = authed_page.locator("[data-testid='series-grid-card']").first
        assert first_card.locator(".series-wall-card-title").is_visible()
        assert first_card.locator(".series-wall-card-meta").count() == 0
        assert first_card.locator("[data-testid='series-grid-hover-meta']").count() == 1
        assert first_card.locator("[data-testid='series-grid-hover-publisher']").count() == 1
        assert first_card.locator("[data-testid='series-grid-hover-years']").count() == 1
        assert first_card.locator("[data-testid='series-grid-hover-type']").count() == 0
        assert first_card.locator("[data-testid='series-grid-hover-owned']").count() == 1
        assert first_card.locator(".series-wall-monitor-dot").is_visible()
        assert (
            first_card.locator("[data-testid='series-monitored-indicator']").get_attribute(
                "aria-label"
            )
            == "Monitored"
        )
        assert first_card.locator(".series-wall-ring").is_visible()
        assert first_card.locator(".series-wall-overlay").count() == 1
        cover_frame_alignment = authed_page.evaluate(
            """() => {
                const card = document.querySelector("[data-testid='series-grid-card']");
                const frame = card ? card.querySelector(".series-wall-cover-wrap") : null;
                if (!card || !frame) {
                    return null;
                }
                const cardRect = card.getBoundingClientRect();
                const frameRect = frame.getBoundingClientRect();
                return {
                    topGap: Math.abs(frameRect.top - cardRect.top),
                    leftGap: Math.abs(frameRect.left - cardRect.left),
                    rightGap: Math.abs(frameRect.right - cardRect.right),
                };
            }"""
        )
        assert cover_frame_alignment is not None
        assert cover_frame_alignment["topGap"] <= 1.5
        assert cover_frame_alignment["leftGap"] <= 1.5
        assert cover_frame_alignment["rightGap"] <= 1.5
        first_checkbox = first_card.locator("[data-testid='series-row-checkbox']").first
        assert first_checkbox.is_visible() is False

        series.open_select_mode()
        assert first_checkbox.is_visible() is True

        checkbox_alignment = authed_page.evaluate(
            """() => {
                const frame = document.querySelector(".series-wall-cover-wrap");
                const checkbox = document.querySelector("[data-testid='series-grid-card'] [data-testid='series-row-checkbox']");
                if (!frame || !checkbox) {
                    return null;
                }
                const frameRect = frame.getBoundingClientRect();
                const checkboxRect = checkbox.getBoundingClientRect();
                return {
                    insideFrame: checkboxRect.top >= frameRect.top
                        && checkboxRect.left >= frameRect.left
                        && checkboxRect.bottom <= frameRect.bottom
                        && checkboxRect.right <= frameRect.right,
                    topInset: checkboxRect.top - frameRect.top,
                    leftInset: checkboxRect.left - frameRect.left,
                };
            }"""
        )
        assert checkbox_alignment is not None
        assert checkbox_alignment["insideFrame"] is True
        assert checkbox_alignment["topInset"] <= 24
        assert checkbox_alignment["leftInset"] >= 0

        series.exit_select_mode()

        first_cover = authed_page.locator("[data-testid='series-grid-cover']").first
        assert first_cover.get_attribute("src")
        assert first_cover.get_attribute("src").startswith("/api/v1/series/")
        assert first_cover.get_attribute("loading") == "eager"
        assert first_cover.get_attribute("fetchpriority") == "high"
        assert first_cover.get_attribute("decoding") == "sync"
        _wait_for_first_grid_cover_loaded(authed_page)
        first_cover_state = first_cover.evaluate(
            """(img) => ({
                complete: img.complete,
                naturalWidth: img.naturalWidth,
                display: window.getComputedStyle(img).display,
            })"""
        )
        assert first_cover_state["complete"] is True
        assert first_cover_state["naturalWidth"] > 0
        assert first_cover_state["display"] != "none"

        series.search("Batman")
        assert series.query_param("q") == "Batman"
        assert series.current_view() == "grid"
        assert authed_page.locator("[data-testid='series-collector-wall-view']").first.is_visible()

        searched_cover_stats = _grid_cover_visibility_stats(authed_page)
        assert searched_cover_stats["total"] > 0
        assert searched_cover_stats["loaded"] == searched_cover_stats["total"]

    def test_list_and_grid_views_use_monitored_indicator_without_text_label(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=5")

        series.choose_view("list")
        assert series.row_has_monitored_indicator("Batman") is True
        assert series.row_has_monitored_indicator("Batman Beyond") is False
        assert "Monitored" not in (series.row_for_title("Batman").text_content() or "")

        series.choose_view("grid")
        assert series.row_has_monitored_indicator("Batman") is True
        assert series.row_has_monitored_indicator("Batman Beyond") is False
        assert "Monitored" not in (series.row_for_title("Batman").text_content() or "")

    def test_grid_cover_link_opens_series_detail(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        series.choose_view("grid")

        first_title = (series.visible_series_links().first.text_content() or "").strip()
        assert first_title

        series.open_first_grid_cover()

        assert "/series/" in authed_page.url
        assert authed_page.locator("text=All Series").first.is_visible()
        assert authed_page.locator("h1, h2").filter(has_text=first_title).first.is_visible()

    def test_grid_cover_fills_frame_without_inset_padding(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        series.choose_view("grid")
        _wait_for_first_grid_cover_loaded(authed_page)
        authed_page.locator("[data-testid='series-grid-card']").first.hover()
        authed_page.wait_for_timeout(220)

        cover_alignment = authed_page.evaluate(
            """() => {
                const frame = document.querySelector("[data-testid='series-grid-cover-frame']");
                const image = document.querySelector("[data-testid='series-grid-cover']");
                const overlay = document.querySelector(".series-wall-overlay");
                if (!frame || !image || !overlay) {
                    return null;
                }
                const frameRect = frame.getBoundingClientRect();
                const imageRect = image.getBoundingClientRect();
                const overlayRect = overlay.getBoundingClientRect();
                const style = getComputedStyle(image);
                const overlayStyle = getComputedStyle(overlay);
                const html = document.documentElement;
                const previousTheme = html.getAttribute("data-theme");
                html.setAttribute("data-theme", "dark");
                const darkOverlayEdgeGuard = getComputedStyle(overlay).boxShadow;
                html.setAttribute("data-theme", "light");
                const lightOverlayEdgeGuard = getComputedStyle(overlay).boxShadow;
                if (previousTheme === null) {
                    html.removeAttribute("data-theme");
                } else {
                    html.setAttribute("data-theme", previousTheme);
                }
                return {
                    topGap: Math.abs(imageRect.top - frameRect.top),
                    leftGap: Math.abs(imageRect.left - frameRect.left),
                    rightGap: Math.abs(frameRect.right - imageRect.right),
                    bottomGap: Math.abs(frameRect.bottom - imageRect.bottom),
                    overlayTopInset: overlayRect.top - frameRect.top,
                    overlayLeftInset: overlayRect.left - frameRect.left,
                    overlayRightInset: frameRect.right - overlayRect.right,
                    overlayBottomInset: frameRect.bottom - overlayRect.bottom,
                    overlayBackgroundColor: overlayStyle.backgroundColor,
                    overlayBackdropFilter: overlayStyle.backdropFilter,
                    overlayRadius: overlayStyle.borderTopLeftRadius,
                    overlayEdgeGuard: overlayStyle.boxShadow,
                    darkOverlayEdgeGuard,
                    lightOverlayEdgeGuard,
                    objectFit: style.objectFit,
                    objectPosition: style.objectPosition,
                    paddingTop: style.paddingTop,
                };
            }"""
        )

        assert cover_alignment is not None
        assert cover_alignment["topGap"] <= 1.5
        assert cover_alignment["leftGap"] <= 1.5
        assert cover_alignment["rightGap"] <= 1.5
        assert cover_alignment["bottomGap"] <= 1.5
        assert cover_alignment["overlayTopInset"] <= -3
        assert cover_alignment["overlayLeftInset"] <= -3
        assert cover_alignment["overlayRightInset"] <= -3
        assert cover_alignment["overlayBottomInset"] <= -3
        assert cover_alignment["overlayBackgroundColor"] == "rgba(30, 26, 23, 0.88)"
        assert cover_alignment["overlayBackdropFilter"] == "blur(6px)"
        assert cover_alignment["overlayRadius"] == "18px"
        assert "inset" in cover_alignment["overlayEdgeGuard"]
        assert "4.5px" in cover_alignment["overlayEdgeGuard"]
        assert "rgb(255, 255, 255)" in cover_alignment["darkOverlayEdgeGuard"]
        assert "rgb(30, 26, 23)" in cover_alignment["lightOverlayEdgeGuard"]
        assert cover_alignment["objectFit"] == "cover"
        assert cover_alignment["objectPosition"] == "50% 0%"
        assert cover_alignment["paddingTop"] == "0px"

    def test_list_and_grid_acquisition_progress_use_visible_tones(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=5")

        list_progress = authed_page.evaluate(
            """() => {
                const fill = document.querySelector(".series-mission-control-bar-fill");
                if (!fill) {
                    return null;
                }
                const style = window.getComputedStyle(fill);
                return {
                    backgroundImage: style.backgroundImage,
                    backgroundColor: style.backgroundColor,
                    width: fill.getBoundingClientRect().width,
                };
            }"""
        )

        assert list_progress is not None
        assert list_progress["width"] > 0
        assert "gradient" in list_progress["backgroundImage"]

        series.choose_view("grid")
        grid_progress = authed_page.evaluate(
            """() => {
                const fill = document.querySelector(".series-wall-ring-fill");
                const bg = document.querySelector(".series-wall-ring-bg");
                if (!fill || !bg) {
                    return null;
                }
                const fillStyle = window.getComputedStyle(fill);
                const bgStyle = window.getComputedStyle(bg);
                return {
                    stroke: fillStyle.stroke,
                    bgStroke: bgStyle.stroke,
                };
            }"""
        )

        assert grid_progress is not None
        assert grid_progress["stroke"] not in ("none", "rgba(0, 0, 0, 0)")
        assert grid_progress["stroke"] != grid_progress["bgStroke"]

    def test_paused_complete_series_keeps_green_completion_tone(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        list_contract = authed_page.evaluate(
            """() => {
                const row = Array.from(document.querySelectorAll("[data-testid='series-result-row']")).find(
                    (el) => (el.textContent || '').includes('Planetary')
                );
                const dot = row?.querySelector("[data-testid='series-monitored-indicator']");
                const fill = row?.querySelector(".series-mission-control-bar-fill");
                const pct = row?.querySelector(".series-mission-control-bar-pct");
                if (!row || !dot || !fill || !pct) {
                    return null;
                }
                return {
                    dotClass: dot.className,
                    fillClass: fill.className,
                    pctClass: pct.className,
                    pctText: pct.textContent,
                };
            }"""
        )

        assert list_contract is not None
        assert "series-led-off" in list_contract["dotClass"]
        assert "series-mission-control-bar-fill-green" in list_contract["fillClass"]
        assert "series-mission-control-bar-pct-green" in list_contract["pctClass"]
        assert list_contract["pctText"] == "100%"

        series.choose_view("grid")

        grid_contract = authed_page.evaluate(
            """() => {
                const card = Array.from(document.querySelectorAll("[data-testid='series-grid-card']")).find(
                    (el) => (el.textContent || '').includes('Planetary')
                );
                const ring = card?.querySelector(".series-wall-ring-fill");
                if (!card || !ring) {
                    return null;
                }
                return {
                    ringClass: ring.className.baseVal || ring.className,
                    ringText: card.querySelector(".series-wall-ring-center")?.textContent,
                };
            }"""
        )

        assert grid_contract is not None
        assert "series-wall-ring-fill-green" in grid_contract["ringClass"]
        assert grid_contract["ringText"] == "100"

    def test_list_view_places_lifecycle_status_after_year(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25", preferred_view="list")

        headers = [
            value.strip().lower()
            for value in authed_page.locator(
                "[data-testid='series-mission-control-table'] thead th"
            ).all_inner_texts()
        ]
        status_cells = authed_page.locator("[data-testid='series-lifecycle-status']")

        assert headers.index("status") == headers.index("year") + 1
        assert headers.index("owned") == headers.index("status") + 1
        assert status_cells.count() > 0
        assert status_cells.first.is_visible()
        assert status_cells.first.inner_text().strip().lower() in {
            "continuing",
            "ended",
            "unknown",
        }

    def test_series_toolbar_dropdown_chevron_stays_tight_to_trigger_edge(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        chevron_spacing = authed_page.evaluate(
            """() => {
                const root = document.querySelector("[data-testid='series-status-select']");
                const trigger = root?.querySelector(".dropdown-select-trigger");
                const chevron = root?.querySelector(".dropdown-select-chevron");
                if (!trigger || !chevron) {
                    return null;
                }
                const triggerRect = trigger.getBoundingClientRect();
                const chevronRect = chevron.getBoundingClientRect();
                return {
                    rightGap: triggerRect.right - chevronRect.right,
                    leftGap: chevronRect.left - triggerRect.left,
                };
            }"""
        )

        assert chevron_spacing is not None
        assert chevron_spacing["rightGap"] <= 16

    def test_ended_series_stays_readable_in_grid_view_without_monitored_copy(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("status=ended&per_page=2")

        series.choose_view("grid")

        ended_card = authed_page.evaluate(
            """() => {
                const card = Array.from(document.querySelectorAll("[data-testid='series-grid-card']"))
                  .find((el) => el.textContent?.includes("Batman Beyond"));
                if (!card) {
                    return null;
                }
                return {
                    text: card.textContent || "",
                    hasMonitorBadge: Boolean(card.querySelector(".series-wall-monitor-dot")),
                };
            }"""
        )

        assert ended_card is not None
        assert "Batman Beyond" in ended_card["text"]
        assert "Monitored" not in ended_card["text"]
        assert ended_card["hasMonitorBadge"] is False

    def test_grid_view_packs_more_cards_per_row_on_wide_screens(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1720, "height": 1200})
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=5")

        series.choose_view("grid")

        first_row_count = authed_page.evaluate(
            """() => {
                const cards = Array.from(document.querySelectorAll("[data-testid='series-grid-card']"));
                if (!cards.length) {
                    return 0;
                }
                const boxes = cards.map((card) => card.getBoundingClientRect());
                const firstTop = boxes[0].top;
                return boxes.filter((box) => Math.abs(box.top - firstTop) < 4).length;
            }"""
        )

        assert first_row_count >= 5

    def test_search_submit_updates_url_and_results(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        series.search("Batman")

        assert _query_param(authed_page.url, "q") == "Batman"
        assert "Batman" in (series.get_series_count_text() or "")
        assert series.visible_series_links().count() >= 1

    def test_filters_and_per_page_apply_immediately_and_reset_paging(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2&page=2")

        assert series.query_param("page") == "2"

        series.set_filter("status", "continuing")
        assert series.query_param("status") == "continuing"
        assert series.query_param("page") in (None, "1")

        series.set_filter("monitored", "true")
        assert series.query_param("monitored") == "true"
        assert series.query_param("page") in (None, "1")

        series.set_filter("sort", "-title")
        assert series.query_param("sort") == "-title"
        assert series.query_param("page") in (None, "1")

        series.set_filter("per-page", "50")
        assert series.query_param("per_page") == "50"
        assert series.query_param("page") in (None, "1")

    def test_sorting_updates_series_link_targets_before_navigation(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        series.set_filter("sort", "-title")
        first_title = series.first_visible_series_title()
        first_href = series.first_visible_series_href()

        assert first_title == "Wonder Woman"
        assert first_href == "/series/5"

        series.open_first_series()

        assert authed_page.url.endswith("/series/5")
        assert authed_page.locator("h1, h2").filter(has_text=first_title).first.is_visible()

    def test_filter_changes_preserve_shell_and_visible_pagination(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        shell_tokens = authed_page.evaluate(
            """() => {
                const header = document.querySelector("[data-testid='app-header']");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                header.dataset.e2eToken = header.dataset.e2eToken || Math.random().toString(36).slice(2);
                footer.dataset.e2eToken = footer.dataset.e2eToken || Math.random().toString(36).slice(2);
                return { header: header.dataset.e2eToken, footer: footer.dataset.e2eToken };
            }"""
        )

        assert series.pagination.is_visible()

        series.set_filter("status", "continuing")

        tokens_after = authed_page.evaluate(
            """() => ({
                header: document.querySelector("[data-testid='app-header']").dataset.e2eToken,
                footer: document.querySelector("[data-testid='page-footer-dock']").dataset.e2eToken,
            })"""
        )
        assert tokens_after == shell_tokens
        assert series.pagination.is_visible()
        assert series.footer.is_visible()

    def test_pagination_hides_when_all_results_fit_on_one_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        assert not series.pagination.is_visible()
        assert series.footer.is_visible()
        dock_alignment = authed_page.evaluate(
            """() => {
                const dock = document.querySelector("[data-testid='page-dock-inner']");
                const status = document.querySelector("[data-testid='page-dock-status']");
                if (!dock || !status) {
                    return null;
                }
                const dockRect = dock.getBoundingClientRect();
                const statusRect = status.getBoundingClientRect();
                const dockStyle = window.getComputedStyle(dock);
                const paddingRight = parseFloat(dockStyle.paddingRight || "0");
                return {
                    dockRight: dockRect.right,
                    statusRight: statusRect.right,
                    gap: dockRect.right - statusRect.right,
                    paddingRight,
                };
            }"""
        )
        assert dock_alignment is not None
        assert dock_alignment["gap"] <= dock_alignment["paddingRight"] + 2

    def test_pagination_controls_work_repeatedly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        shell_tokens = authed_page.evaluate(
            """() => {
                const header = document.querySelector("[data-testid='app-header']");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                header.dataset.e2eToken = header.dataset.e2eToken || Math.random().toString(36).slice(2);
                footer.dataset.e2eToken = footer.dataset.e2eToken || Math.random().toString(36).slice(2);
                return { header: header.dataset.e2eToken, footer: footer.dataset.e2eToken };
            }"""
        )

        series.click_next_page()
        assert series.query_param("page") == "2"

        series.click_page(3)
        assert series.query_param("page") == "3"

        series.click_prev_page()
        assert series.query_param("page") == "2"

        tokens_after = authed_page.evaluate(
            """() => ({
                header: document.querySelector("[data-testid='app-header']").dataset.e2eToken,
                footer: document.querySelector("[data-testid='page-footer-dock']").dataset.e2eToken,
            })"""
        )
        assert tokens_after == shell_tokens
        assert series.footer.is_visible()
        assert series.results_body.is_visible()

    def test_pagination_replaces_results_body_without_nesting_duplicate_ids(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        assert authed_page.locator("#series-results-body").count() == 1

        series.click_next_page()

        assert series.query_param("page") == "2"
        assert authed_page.locator("#series-results-body").count() == 1

    @pytest.mark.parametrize("view", ["list", "grid"])
    def test_catalog_refresh_preserves_series_selection(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        view: str,
    ) -> None:
        _run_async_blocking(_set_series_catalog_state("Batman", "hydrating"))
        try:
            series = SeriesListPage(authed_page, seeded_server)
            series.goto("q=Batman&per_page=25", preferred_view=view)
            assert series.results_body.get_attribute("hx-swap") == "morph:outerHTML"

            series.toggle_row_selection("Batman")
            assert series.selected_count_text() == "1 selected"
            assert series.row_is_selected("Batman")

            _trigger_series_catalog_refresh(authed_page)

            assert series.selected_count_text() == "1 selected"
            assert series.row_is_selected("Batman")
            assert authed_page.evaluate(
                "() => document.querySelector('#series-results-body').__pbSelectionRefreshSentinel === true"
            )
        finally:
            _run_async_blocking(_set_series_catalog_state("Batman", "complete"))

    def test_pagination_returns_viewport_to_results_top_in_grid_view(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 540, "height": 520})
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")
        series.choose_view("grid")

        series.pagination.scroll_into_view_if_needed()
        _wait_for_animation_frames(authed_page)

        before_scroll = authed_page.evaluate(
            "() => document.querySelector('#content') ? document.querySelector('#content').scrollTop : window.scrollY"
        )
        if before_scroll <= 0:
            authed_page.evaluate(
                """() => {
                    const content = document.querySelector('#content');
                    if (content) {
                        content.scrollTop = content.scrollHeight;
                        return;
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            _wait_for_animation_frames(authed_page)
            before_scroll = authed_page.evaluate(
                "() => document.querySelector('#content') ? document.querySelector('#content').scrollTop : window.scrollY"
            )

        series.click_next_page()

        after_position = authed_page.evaluate(
            """() => {
                const toolbar = document.querySelector("#series-filter-form");
                const content = document.querySelector("#content");
                return {
                    scrollY: content ? content.scrollTop : window.scrollY,
                    toolbarTop: toolbar ? toolbar.getBoundingClientRect().top : null,
                };
            }"""
        )

        assert before_scroll > 0
        assert after_position["scrollY"] < before_scroll
        assert after_position["scrollY"] <= 1
        assert after_position["toolbarTop"] is not None

    def test_bulk_selection_persists_across_pagination_and_can_clear(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        series.toggle_row_selection("Batman")
        assert series.selected_count_text() == "1 selected"
        assert series.row_is_selected("Batman")

        series.click_next_page()
        series.toggle_row_selection("Saga", modifiers=["ControlOrMeta"])
        assert series.selected_count_text() == "2 selected"

        series.click_prev_page()
        assert series.row_is_selected("Batman")

        series.clear_bulk_selection()
        assert series.selected_count_text() == "0 selected"
        assert not series.select_mode_toolbar.is_visible()
        assert not series.row_is_selected("Batman")

    @pytest.mark.parametrize(
        ("view", "additive_modifier"),
        [("list", "ControlOrMeta"), ("grid", "ControlOrMeta")],
    )
    def test_checkbox_selection_supports_file_explorer_modifiers(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        view: str,
        additive_modifier: str,
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("sort=title&per_page=25", preferred_view=view)
        series.open_select_mode()
        checkboxes = authed_page.locator("[data-testid='series-row-checkbox']:visible")
        # Other browser tests add/remove series in the session-scoped seed.
        # Range selection must include every visible item, not a fixed title list.
        visible_ids = checkboxes.evaluate_all("items => items.map(item => item.value)")
        ids = {
            title: authed_page.get_by_role(
                "checkbox", name=f"Select {title}", exact=True
            ).input_value()
            for title in ("Batman", "Planetary", "Saga")
        }

        def inclusive_range(first: str, last: str) -> set[str]:
            start, end = sorted((visible_ids.index(ids[first]), visible_ids.index(ids[last])))
            return set(visible_ids[start : end + 1])

        def assert_selected(expected: set[str]) -> None:
            assert series.selected_count_text() == f"{len(expected)} selected"
            assert (
                set(
                    checkboxes.evaluate_all(
                        "items => items.filter(item => item.checked).map(item => item.value)"
                    )
                )
                == expected
            )

        series.toggle_row_selection("Batman")
        series.toggle_row_selection("Planetary", modifiers=[additive_modifier])
        assert_selected({ids["Batman"], ids["Planetary"]})
        assert series.row_is_selected("Batman")
        assert series.row_is_selected("Planetary")

        series.toggle_row_selection("Saga", modifiers=["Shift"])
        shifted_ids = inclusive_range("Planetary", "Saga")
        assert_selected(shifted_ids)
        assert not series.row_is_selected("Batman")
        assert series.row_is_selected("Planetary")
        assert series.row_is_selected("Saga")

        series.toggle_row_selection(
            "Batman",
            modifiers=[additive_modifier, "Shift"],
        )
        assert_selected(shifted_ids | inclusive_range("Batman", "Planetary"))

        series.toggle_row_selection("Planetary")
        assert_selected(set())
        assert not series.row_is_selected("Planetary")
        assert not series.row_is_selected("Batman")
        assert not series.row_is_selected("Saga")

    def test_toolbar_height_stays_visually_stable_when_switching_modes(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1440, "height": 1000})
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        browse_box = series.toolbar.bounding_box()
        assert browse_box is not None

        series.open_select_mode()
        select_box = series.toolbar.bounding_box()
        assert select_box is not None

        series.exit_select_mode()
        browse_again_box = series.toolbar.bounding_box()
        assert browse_again_box is not None

        assert abs(select_box["height"] - browse_box["height"]) <= 12
        assert abs(browse_again_box["height"] - browse_box["height"]) <= 2

    def test_selection_controls_only_show_in_select_mode_and_done_clears_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        assert series.visible_row_checkbox_count() == 0

        series.open_select_mode()
        assert series.visible_row_checkbox_count() > 0

        series.toggle_row_selection("Batman")
        assert series.selected_count_text() == "1 selected"
        assert series.row_is_selected("Batman")

        series.exit_select_mode()
        assert series.visible_row_checkbox_count() == 0
        assert series.selected_count_text() == "0 selected"
        assert not series.row_is_selected("Batman")

    def test_select_visible_select_all_results_and_deselect_all_work_in_select_mode(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        series.open_select_mode()
        series.select_visible()
        assert series.selected_count_text() == "2 selected"
        assert series.row_is_selected("Batman")
        assert series.row_is_selected("Batman Beyond")

        series.click_next_page()
        assert series.select_visible_disabled() is False
        series.select_visible()
        assert series.selected_count_text() == "4 selected"
        assert series.row_is_selected("Planetary")
        assert series.row_is_selected("Saga")

        series.select_all_results()
        assert series.selected_count_text() == "6 selected"
        assert series.row_is_selected("Planetary")
        assert series.row_is_selected("Saga")

        series.deselect_all_visible()
        assert series.selected_count_text() == "0 selected"
        assert series.toolbar_mode() == "select"
        assert series.visible_row_checkbox_count() > 0
        assert not series.row_is_selected("Planetary")
        assert not series.row_is_selected("Saga")

    def test_done_clears_selection_and_restores_browse_controls(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        series.toggle_row_selection("Batman")
        assert series.selected_count_text() == "1 selected"
        assert series.row_is_selected("Batman")
        assert series.toolbar_mode() == "select"
        assert authed_page.locator("[data-testid='series-view-list']").first.is_visible() is False

        series.exit_select_mode()
        assert series.toolbar_mode() == "browse"
        assert series.selected_count_text() == "0 selected"
        assert not series.row_is_selected("Batman")

        series.choose_view("list")
        assert series.current_view() == "list"
        assert series.visible_row_checkbox_count() == 0

        series.choose_view("grid")
        assert series.current_view() == "grid"
        assert series.visible_row_checkbox_count() == 0

    def test_bulk_monitor_and_unmonitor_refresh_rows_without_shell_swap(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        shell_tokens = authed_page.evaluate(
            """() => {
                const header = document.querySelector("[data-testid='app-header']");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                header.dataset.e2eToken = header.dataset.e2eToken || Math.random().toString(36).slice(2);
                footer.dataset.e2eToken = footer.dataset.e2eToken || Math.random().toString(36).slice(2);
                return { header: header.dataset.e2eToken, footer: footer.dataset.e2eToken };
            }"""
        )

        assert not series.row_has_monitored_indicator("Batman Beyond")
        series.toggle_row_selection("Batman Beyond")
        series.apply_bulk_action("monitor")
        assert series.selected_count_text() == "0 selected"
        assert series.row_has_monitored_indicator("Batman Beyond")

        assert series.row_has_monitored_indicator("Saga")
        series.toggle_row_selection("Saga")
        series.apply_bulk_action("unmonitor")
        assert series.selected_count_text() == "0 selected"
        assert not series.row_has_monitored_indicator("Saga")

        tokens_after = authed_page.evaluate(
            """() => ({
                header: document.querySelector("[data-testid='app-header']").dataset.e2eToken,
                footer: document.querySelector("[data-testid='page-footer-dock']").dataset.e2eToken,
            })"""
        )
        assert tokens_after == shell_tokens
        assert series.footer.is_visible()
        assert series.results_body.is_visible()

    def test_bulk_delete_requires_confirmation_and_removes_rows_without_shell_swap(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        shell_tokens = authed_page.evaluate(
            """() => {
                const header = document.querySelector("[data-testid='app-header']");
                const footer = document.querySelector("[data-testid='page-footer-dock']");
                header.dataset.e2eToken = header.dataset.e2eToken || Math.random().toString(36).slice(2);
                footer.dataset.e2eToken = footer.dataset.e2eToken || Math.random().toString(36).slice(2);
                return { header: header.dataset.e2eToken, footer: footer.dataset.e2eToken };
            }"""
        )

        assert series.row_for_title("Batman Beyond").is_visible()
        assert series.row_for_title("Wonder Woman").is_visible()

        series.toggle_row_selection("Batman Beyond")
        series.toggle_row_selection("Wonder Woman", modifiers=["ControlOrMeta"])
        series.open_bulk_delete_confirm()

        assert series.bulk_delete_confirm_visible()
        assert series.row_for_title("Batman Beyond").is_visible()
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

        series.cancel_delete()
        assert not series.bulk_delete_confirm_visible()
        assert series.row_for_title("Batman Beyond").is_visible()
        assert series.selected_count_text() == "2 selected"

        series.open_bulk_delete_confirm()
        payload = series.confirm_delete()

        assert payload["delete_files"] is False
        assert payload["delete_folder"] is False

        assert series.selected_count_text() == "0 selected"
        assert series.row_count("Batman Beyond") == 0
        assert series.row_count("Wonder Woman") == 0

        tokens_after = authed_page.evaluate(
            """() => ({
                header: document.querySelector("[data-testid='app-header']").dataset.e2eToken,
                footer: document.querySelector("[data-testid='page-footer-dock']").dataset.e2eToken,
            })"""
        )
        assert tokens_after == shell_tokens
        assert series.footer.is_visible()
        assert series.results_body.is_visible()

    def test_bulk_delete_folder_option_forces_delete_files(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        series.toggle_row_selection("Batman")
        series.open_bulk_delete_confirm()

        assert not series.delete_files_checked()
        assert not series.delete_files_disabled()

        series.toggle_delete_folders()
        assert series.delete_files_checked()
        assert series.delete_files_disabled()

        series.toggle_delete_folders()
        assert not series.delete_files_checked()
        assert not series.delete_files_disabled()

        series.cancel_delete()

    def test_row_delete_action_is_removed_from_mission_control_and_grid_cards(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        assert (
            authed_page.locator(
                "[data-testid='series-result-row'] [data-testid='series-row-delete']"
            ).count()
            == 0
        )

        series.choose_view("grid")
        assert (
            authed_page.locator(
                "[data-testid='series-grid-card'] [data-testid='series-row-delete']"
            ).count()
            == 0
        )

    def test_bulk_delete_remains_the_only_delete_path_from_series_list(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=25")

        series.toggle_row_selection("Superman")
        assert series.selected_count_text() == "1 selected"

        series.open_bulk_delete_confirm()
        payload = series.confirm_delete()

        assert payload["delete_files"] is False
        assert payload["delete_folder"] is False
        assert len(payload["series_ids"]) == 1
        assert payload["series_ids"][0] > 0

        assert series.row_count("Superman") == 0
        assert series.selected_count_text() == "0 selected"
        authed_page.locator("#toast-container").get_by_text("Deleted 1 series").first.wait_for(
            state="visible",
            timeout=5000,
        )
        assert (
            "Unable to delete this series right now"
            not in authed_page.locator("#toast-container").text_content()
        )
        assert not series.bulk_delete_confirm_visible()
        assert series.footer.is_visible()
        assert series.results_body.is_visible()

    def test_hard_refresh_and_sidebar_return_preserve_filters_and_view(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("q=Batman&status=continuing&monitored=true&sort=-title&per_page=50")
        series.choose_view("grid")
        assert series.current_view() == "grid"

        authed_page.locator("aside a[href='/downloads']").first.click()
        wait_for_htmx(authed_page)
        assert "/downloads" in authed_page.url

        authed_page.locator("aside a[href^='/series']").first.click()
        wait_for_htmx(authed_page)
        series.wait_until_ready()

        assert _query_param(authed_page.url, "q") == "Batman"
        assert _query_param(authed_page.url, "status") == "continuing"
        assert _query_param(authed_page.url, "monitored") == "true"
        assert _query_param(authed_page.url, "sort") == "-title"
        assert _query_param(authed_page.url, "per_page") == "50"
        assert series.current_view() == "grid"
        assert authed_page.locator("[data-testid='series-collector-wall-view']").first.is_visible()

        authed_page.reload()
        series.wait_until_ready()
        assert series.search_value() == "Batman"
        assert series.selected_value("status") == "continuing"
        assert series.selected_value("monitored") == "true"
        assert series.selected_value("sort") == "-title"
        assert series.selected_value("per-page") == "50"
        assert series.current_view() == "grid"
        assert authed_page.locator("[data-testid='series-collector-wall-view']").first.is_visible()

    def test_search_submit_updates_results(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        series.search("Batman")

        assert _query_param(authed_page.url, "q") == "Batman"
        assert series.row_count("Batman") >= 1
        assert series.visible_series_links().count() >= 1
        assert series.row_count("Saga") == 0

    def test_search_autosubmits_after_typing(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        series.search("Batman")

        assert _query_param(authed_page.url, "q") == "Batman"
        assert series.row_count("Batman") >= 1
        assert series.visible_series_links().count() >= 1
        assert series.row_count("Saga") == 0

    def test_search_history_reuses_recent_query(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()
        authed_page.evaluate(
            """() => {
                localStorage.setItem(
                    'pullbox.searchHistory.series',
                    JSON.stringify(['Batman'])
                );
            }"""
        )

        series.search_input.click()
        history_panel = authed_page.locator("[data-testid='series-search-history-panel']").first
        history_panel.wait_for(state="visible")
        history_panel.locator("[data-search-history-item]", has_text="Batman").first.click()
        series.wait_for_query_param("q", "Batman")

        assert _query_param(authed_page.url, "q") == "Batman"
        assert series.search_value() == "Batman"
        assert series.row_count("Batman") >= 1
        assert series.visible_series_links().count() >= 1

    def test_backspace_to_empty_keeps_history_open(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.add_init_script(
            """() => {
                localStorage.removeItem('pullbox.searchHistory.series');
            }"""
        )
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        series.search("Batman")
        assert _query_param(authed_page.url, "q") == "Batman"

        series.search_input.click()
        authed_page.keyboard.press("ControlOrMeta+A")
        authed_page.keyboard.press("Backspace")
        series.wait_for_query_param("q", None)
        wait_for_htmx(authed_page)

        history_panel = authed_page.locator("[data-testid='series-search-history-panel']").first
        history_panel.wait_for(state="visible")

        assert series.search_value() == ""
        assert _query_param(authed_page.url, "q") in (None, "")
        assert history_panel.locator(
            "[data-search-history-item]", has_text="Batman"
        ).first.is_visible()

    def test_clear_search_resets_to_unfiltered_results(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()

        series.search_input.fill("Batman")
        series.search_clear.wait_for(state="visible")
        assert series.search_clear.evaluate("el => getComputedStyle(el).position") == "absolute"
        field_box = authed_page.locator("[data-testid='series-search-field']").first.bounding_box()
        clear_box = series.search_clear.bounding_box()
        assert field_box is not None
        assert clear_box is not None
        assert field_box["y"] <= clear_box["y"] <= field_box["y"] + field_box["height"]
        series.wait_for_query_param("q", "Batman")
        wait_for_htmx(authed_page)

        assert _query_param(authed_page.url, "q") == "Batman"
        assert series.row_count("Batman") >= 1
        assert series.visible_series_links().count() >= 1

        series.clear_search()

        assert series.search_value() == ""
        assert _query_param(authed_page.url, "q") in (None, "")
        assert series.visible_series_links().count() > 1

    def test_detail_back_link_restores_last_list_url(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("status=continuing&per_page=2&page=2")

        series.open_first_series()

        back_link = authed_page.locator("a[data-series-index-link='true']").first
        assert back_link.is_visible()
        assert "status=continuing" in (back_link.get_attribute("href") or "")
        assert "per_page=2" in (back_link.get_attribute("href") or "")
        assert "page=2" in (back_link.get_attribute("href") or "")

        back_link.click()
        wait_for_htmx(authed_page)
        series.wait_until_ready()

        assert _query_param(authed_page.url, "status") == "continuing"
        assert _query_param(authed_page.url, "per_page") == "2"
        assert _query_param(authed_page.url, "page") == "2"

    def test_empty_state_and_mobile_shell_smoke(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 430, "height": 932})
        series = SeriesListPage(authed_page, seeded_server)
        series.goto()
        series.search("TotallyMissingSeries")

        assert authed_page.locator("[data-testid='series-empty-state']").first.is_visible()
        assert series.toolbar.is_visible()
        assert series.footer.is_visible()

    def test_mobile_toolbar_and_two_view_controls_remain_usable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 430, "height": 932})
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")

        layout_metrics = authed_page.evaluate(
            """() => {
                const selectors = {
                    toolbar: "[data-testid='series-toolbar']",
                    viewToggle: "[data-testid='series-view-toggle']",
                    selectToggle: "[data-testid='series-select-mode-toggle']",
                };
                const result = {};
                for (const [key, selector] of Object.entries(selectors)) {
                    const el = document.querySelector(selector);
                    result[key] = el
                        ? {
                            width: el.clientWidth,
                            overflow: el.scrollWidth > el.clientWidth + 1,
                          }
                        : null;
                }
                return result;
            }"""
        )

        assert layout_metrics["toolbar"] is not None
        assert layout_metrics["viewToggle"] is not None
        assert layout_metrics["selectToggle"] is not None
        assert layout_metrics["toolbar"]["overflow"] is False
        assert layout_metrics["viewToggle"]["overflow"] is False
        assert layout_metrics["selectToggle"]["overflow"] is False

        series.open_select_mode()
        select_mode_metrics = authed_page.evaluate(
            """() => {
                const el = document.querySelector("[data-testid='series-bulk-actions']");
                return el
                    ? {
                        width: el.clientWidth,
                        overflow: el.scrollWidth > el.clientWidth + 1,
                      }
                    : null;
            }"""
        )

        assert select_mode_metrics is not None
        assert select_mode_metrics["overflow"] is False

        series.exit_select_mode()
        series.choose_view("list")
        assert authed_page.locator("[data-testid='series-mission-control-view']").first.is_visible()
        assert series.visible_row_checkbox_count() == 0

        series.open_select_mode()
        assert series.visible_row_checkbox_count() > 0

        series.toggle_row_selection("Batman")
        assert series.selected_count_text() == "1 selected"

        assert series.footer.is_visible()
        assert series.results_body.is_visible()
        assert authed_page.locator("[data-testid='series-mission-control-view']").first.is_visible()

        series.exit_select_mode()
        assert series.selected_count_text() == "0 selected"

        series.choose_view("grid")
        assert authed_page.locator("[data-testid='series-collector-wall-view']").first.is_visible()
        assert authed_page.locator("[data-testid='series-grid-card']").count() > 0
        assert authed_page.locator("[data-testid='series-grid-cover']").count() > 0
        assert (
            authed_page.locator(
                "[data-testid='series-grid-card'] [data-testid='series-row-delete']"
            ).count()
            == 0
        )
        grid_metrics = authed_page.evaluate(
            """() => {
                const card = document.querySelector("[data-testid='series-grid-card']");
                return card
                    ? {
                        overflow: card.scrollWidth > card.clientWidth + 1,
                        width: card.clientWidth,
                      }
                    : null;
            }"""
        )
        assert grid_metrics is not None
        assert grid_metrics["overflow"] is False

    def test_results_body_never_goes_blank_during_updates(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=1")
        _install_series_blank_monitor(authed_page)

        authed_page.evaluate("() => window.__pbSeriesBlankMonitor.start()")
        series.click_next_page()
        _wait_for_animation_frames(authed_page)
        pagination_stats = authed_page.evaluate("() => window.__pbSeriesBlankMonitor.stop()")
        _assert_no_blank_stats(pagination_stats)

        authed_page.evaluate("() => window.__pbSeriesBlankMonitor.start()")
        series.search("Batman")
        _wait_for_animation_frames(authed_page)
        search_stats = authed_page.evaluate("() => window.__pbSeriesBlankMonitor.stop()")
        _assert_no_blank_stats(search_stats)

        authed_page.evaluate("() => window.__pbSeriesBlankMonitor.start()")
        series.choose_view("grid")
        _wait_for_animation_frames(authed_page)
        grid_stats = authed_page.evaluate("() => window.__pbSeriesBlankMonitor.stop()")
        _assert_no_blank_stats(grid_stats)

    def test_view_toggle_keeps_list_visible_until_grid_bundle_is_ready(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        series = SeriesListPage(authed_page, seeded_server)
        series.goto("per_page=2")
        _install_series_blank_monitor(authed_page)

        def delay_cover(route) -> None:  # type: ignore[no-untyped-def]
            time.sleep(0.3)
            route.continue_()

        authed_page.route("**/api/v1/series/*/cover", delay_cover)

        authed_page.evaluate("() => window.__pbSeriesBlankMonitor.start()")
        authed_page.locator("[data-testid='series-view-grid']").first.click()
        authed_page.wait_for_timeout(120)

        list_view = authed_page.locator("[data-testid='series-mission-control-view']").first
        list_still_visible = list_view.count() > 0 and list_view.is_visible()
        early_grid_stats = _grid_cover_visibility_stats(authed_page)

        assert list_still_visible or early_grid_stats["loaded"] > 0, early_grid_stats

        authed_page.locator("[data-testid='series-collector-wall-view']").first.wait_for(
            state="visible",
            timeout=5000,
        )
        _wait_for_first_grid_cover_loaded(authed_page)
        _wait_for_animation_frames(authed_page)
        stats = authed_page.evaluate("() => window.__pbSeriesBlankMonitor.stop()")
        _assert_no_blank_stats(stats)
