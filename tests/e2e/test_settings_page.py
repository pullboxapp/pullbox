"""Focused browser coverage for the rewritten settings shell."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from pullbox.config import get_settings
from pullbox.utilities.settings import resolve_utility_directory
from tests.e2e.pages.settings import SettingsPage

pytestmark = pytest.mark.e2e


class TestSettingsPage:
    """Behavior-first E2E checks for the settings shell."""

    def test_media_library_root_manager_previews_and_adds_a_root(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        roots = [
            {
                "id": 700,
                "name": "Primary",
                "path": "/comics",
                "enabled": True,
                "allow_referenced_registrations": True,
                "allow_managed_writes": True,
                "is_default_managed_destination": True,
                "available": True,
                "readable": True,
                "writable": True,
                "free_bytes": 10 * 1024**3,
                "status": "ready",
                "warnings": [],
                "can_disable": False,
            }
        ]
        create_requests: list[dict[str, Any]] = []

        def handle_roots(route) -> None:  # type: ignore[no-untyped-def]
            if route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(roots),
                )
                return

            payload = route.request.post_data_json
            create_requests.append(payload)
            created = {
                "id": 701,
                **payload,
                "enabled": True,
                "available": True,
                "readable": True,
                "writable": False,
                "free_bytes": 8 * 1024**3,
                "status": "read_only",
                "warnings": ["Directory is read-only to Pullbox."],
                "can_disable": True,
            }
            roots.append(created)
            route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps(created),
            )

        def handle_preview(route) -> None:  # type: ignore[no-untyped-def]
            payload = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        **payload,
                        "available": True,
                        "readable": True,
                        "writable": False,
                        "free_bytes": 8 * 1024**3,
                        "status": "read_only",
                        "warnings": ["Directory is read-only to Pullbox."],
                        "blocking_reasons": [],
                        "can_create": True,
                    }
                ),
            )

        authed_page.route("**/api/v1/config/library-roots/preview", handle_preview)
        authed_page.route("**/api/v1/config/library-roots", handle_roots)

        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        manager = authed_page.get_by_test_id("settings-media-library-roots")
        manager.get_by_text("Primary", exact=True).wait_for(state="visible", timeout=5000)
        authed_page.get_by_test_id("settings-media-library-root-name").fill("Archive")
        authed_page.get_by_test_id("settings-media-library-root-path").fill("/archive")
        authed_page.get_by_test_id("settings-media-library-root-managed-role").locator(
            "xpath=.."
        ).click()

        manager.get_by_text("Ready to add", exact=True).wait_for(state="visible", timeout=5000)
        manager.get_by_role("button", name="Add library root").click()

        archive = authed_page.get_by_test_id("settings-media-library-root-701")
        archive.get_by_text("Archive", exact=True).wait_for(state="visible", timeout=5000)
        assert archive.get_by_text("Read only", exact=True).is_visible()
        assert create_requests == [
            {
                "name": "Archive",
                "path": "/archive",
                "allow_referenced_registrations": True,
                "allow_managed_writes": False,
                "is_default_managed_destination": False,
            }
        ]

    def test_settings_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        assert settings.page_root.is_visible()
        assert settings.header.is_visible()
        assert settings.page_title.is_visible()
        assert settings.body.is_visible()
        assert settings.tabs.is_visible()
        assert settings.content.is_visible()
        assert settings.footer_dock.is_visible()
        assert authed_page.locator("[data-testid='page-dock-inner']").first.is_visible()
        assert settings.panel("general").is_visible()
        assert settings.tab("general").get_attribute("aria-current") == "page"

    def test_settings_header_matches_series_header_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        settings_title = authed_page.locator("[data-testid='settings-page-title']").first
        settings_subtitle = authed_page.locator("[data-testid='settings-page-subtitle']").first

        settings_title_style = settings_title.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                lineHeight: style.lineHeight,
                textTransform: style.textTransform,
              };
            }
            """
        )
        settings_subtitle_style = settings_subtitle.evaluate(
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

        authed_page.goto(f"{seeded_server}/series")
        authed_page.wait_for_load_state("networkidle")

        series_title = authed_page.locator("[data-testid='series-registry-title']").first
        series_subtitle = authed_page.locator("[data-testid='series-registry-subtitle']").first

        series_title_style = series_title.evaluate(
            """
            node => {
              const style = window.getComputedStyle(node);
              return {
                fontFamily: style.fontFamily,
                fontSize: style.fontSize,
                fontWeight: style.fontWeight,
                letterSpacing: style.letterSpacing,
                lineHeight: style.lineHeight,
                textTransform: style.textTransform,
              };
            }
            """
        )
        series_subtitle_style = series_subtitle.evaluate(
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

        assert settings_title_style == series_title_style
        assert settings_subtitle_style == series_subtitle_style

    def test_settings_tab_switch_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        settings.switch_tab("utilities")

        assert settings.page_root.is_visible()
        assert authed_page.locator("[data-testid='settings-page']").count() == 1
        assert authed_page.locator("[data-testid='settings-body']").count() == 1
        assert authed_page.locator("[data-testid='settings-tabs']").count() == 1
        assert authed_page.locator("[data-testid='settings-content']").count() == 1
        assert settings.footer_dock.is_visible()
        assert authed_page.locator("[data-testid='page-dock-inner']").first.is_visible()
        assert settings.panel("utilities").is_visible()
        assert settings.tab("utilities").get_attribute("aria-current") == "page"

        settings.switch_tab("ui")

        assert settings.page_root.is_visible()
        assert authed_page.locator("[data-testid='settings-page']").count() == 1
        assert authed_page.locator("[data-testid='settings-body']").count() == 1
        assert authed_page.locator("[data-testid='settings-tabs']").count() == 1
        assert authed_page.locator("[data-testid='settings-content']").count() == 1
        assert settings.footer_dock.is_visible()
        assert authed_page.locator("[data-testid='page-dock-inner']").first.is_visible()
        assert settings.panel("ui").is_visible()
        assert settings.tab("ui").get_attribute("aria-current") == "page"

    def test_settings_card_footer_keeps_clearance_above_page_dock(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        gap = authed_page.evaluate(
            """
            () => {
              const content = document.querySelector("#content");
              const pageDock = document.querySelector("#page-footer-dock");
              const cardFooters = Array.from(document.querySelectorAll(".settings-footer"));
              const lastCardFooter = cardFooters.at(-1);
              if (!content || !pageDock || !lastCardFooter) return null;
              content.scrollTop = content.scrollHeight;
              const footerBox = lastCardFooter.getBoundingClientRect();
              const dockBox = pageDock.getBoundingClientRect();
              return dockBox.top - footerBox.bottom;
            }
            """
        )

        assert gap is not None
        assert gap >= 12

    def test_general_toggles_do_not_expand_footers(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 900, "height": 760})
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("general")

        def settings_viewport_metrics() -> dict[str, object]:
            return authed_page.evaluate(
                """
                () => {
                  const content = document.querySelector("#content");
                  const dock = document.querySelector("#page-footer-dock");
                  const footers = Array.from(
                    document.querySelectorAll("[data-testid='settings-panel-general'] .settings-footer")
                  );
                  const cards = Array.from(
                    document.querySelectorAll("[data-testid='settings-panel-general'] .section-card")
                  );
                  return {
                    scrollTop: content ? Math.round(content.scrollTop) : -1,
                    dockHeight: dock ? Math.round(dock.getBoundingClientRect().height) : -1,
                    maxCardFooterHeight: Math.max(
                      0,
                      ...footers.map((footer) => Math.round(footer.getBoundingClientRect().height))
                    ),
                    visibleCardCount: cards.filter((card) => {
                      const box = card.getBoundingClientRect();
                      return box.bottom > 0 && box.top < window.innerHeight;
                    }).length,
                  };
                }
                """
            )

        authed_page.locator(
            "[data-testid='settings-general-https-card']"
        ).scroll_into_view_if_needed()
        before = settings_viewport_metrics()
        assert before["dockHeight"] <= 90
        assert before["maxCardFooterHeight"] <= 90
        assert before["visibleCardCount"] >= 1

        authed_page.locator("[data-testid='settings-general-https-enabled']").evaluate(
            "el => el.click()"
        )
        authed_page.wait_for_function(
            """
            () => {
              const input = document.querySelector("[data-testid='settings-general-https-enabled']");
              const reset = input
                ?.closest(".section-card")
                ?.querySelector(".settings-footer button[type='button']");
              return !!reset && window.getComputedStyle(reset).display !== "none";
            }
            """,
            timeout=5000,
        )

        https_metrics = settings_viewport_metrics()
        assert abs(int(https_metrics["scrollTop"]) - int(before["scrollTop"])) <= 1
        assert https_metrics["dockHeight"] <= 90
        assert https_metrics["maxCardFooterHeight"] <= 90
        assert https_metrics["visibleCardCount"] >= 1

        authed_page.locator(
            "[data-testid='settings-general-usage-stats-toggle']"
        ).scroll_into_view_if_needed()
        before_usage = settings_viewport_metrics()
        authed_page.locator("[data-testid='settings-general-usage-stats-toggle']").evaluate(
            "el => el.click()"
        )
        authed_page.wait_for_function(
            """
            () => {
              const input = document.querySelector("[data-testid='settings-general-usage-stats-toggle']");
              const reset = input
                ?.closest(".section-card")
                ?.querySelector(".settings-footer button[type='button']");
              return !!reset && window.getComputedStyle(reset).display !== "none";
            }
            """,
            timeout=5000,
        )

        usage_metrics = settings_viewport_metrics()
        assert abs(int(usage_metrics["scrollTop"]) - int(before_usage["scrollTop"])) <= 1
        assert usage_metrics["dockHeight"] <= 90
        assert usage_metrics["maxCardFooterHeight"] <= 90
        assert usage_metrics["visibleCardCount"] >= 1

    def test_settings_direct_tab_load_renders_matching_panel(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("clients")

        assert settings.page_root.is_visible()
        assert settings.panel("clients").is_visible()
        assert settings.tab("clients").get_attribute("aria-current") == "page"

    def test_settings_clients_registry_uses_header_actions_and_single_stack(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("clients")

        panel = settings.panel("clients")
        assert panel.locator("[data-testid='settings-clients-registry-card']").first.is_visible()
        assert panel.locator("[data-testid='settings-clients-registry-actions']").first.is_visible()
        assert panel.locator("[data-testid='settings-clients-test-all']").first.is_visible()
        assert panel.locator("[data-testid='settings-clients-add-client']").first.is_visible()
        assert panel.locator("[data-testid='settings-clients-registry-list']").first.is_visible()
        assert panel.get_by_text("Registry notes", exact=True).count() == 0
        assert panel.get_by_text("Path mapping help", exact=True).count() == 0
        assert panel.get_by_text("Add another client", exact=True).count() == 0

    def test_settings_indexers_registry_matches_clients_style_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        panel = settings.panel("indexers")
        assert panel.locator("[data-testid='settings-indexers-prowlarr-card']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-jackett-card']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-registry-card']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-test-all']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-add-indexer']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-registry-list']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-failure-card']").first.is_visible()
        assert panel.locator("[data-testid='settings-indexers-blocklist-card']").first.is_visible()
        assert panel.get_by_text("Registry rules", exact=True).count() == 0
        assert panel.get_by_text("Registry snapshot", exact=True).count() == 0
        assert panel.get_by_text("Add another source", exact=True).count() == 0
        assert panel.get_by_text("Before you raise the threshold", exact=True).count() == 0
        assert panel.get_by_text("When this helps most", exact=True).count() == 0

    def test_settings_indexer_cards_keep_standard_vertical_gap(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        gaps = authed_page.evaluate(
            """
            () => {
              const box = (testId) => document
                .querySelector(`[data-testid='${testId}']`)
                ?.getBoundingClientRect();
              const registry = box("settings-indexers-registry-card");
              const priority = box("settings-indexers-priority-card");
              const failure = box("settings-indexers-failure-card");
              if (!registry || !priority || !failure) return null;
              return {
                registryToPriority: Math.round(priority.top - registry.bottom),
                priorityToFailure: Math.round(failure.top - priority.bottom),
              };
            }
            """
        )

        assert gaps is not None
        assert gaps["registryToPriority"] == gaps["priorityToFailure"] == 24

    def test_settings_indexers_prowlarr_save_sync_requires_dirty_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        save_button = authed_page.locator(
            "[data-testid='settings-indexers-prowlarr-save-sync']"
        ).first
        url_input = authed_page.locator("[data-testid='settings-indexers-prowlarr-url']").first
        api_key_input = authed_page.locator(
            "[data-testid='settings-indexers-prowlarr-api-key']"
        ).first

        assert save_button.is_disabled()

        original_value = url_input.input_value()
        updated_value = (
            f"{original_value.rstrip('/')}/alt" if original_value else "http://127.0.0.1:9696"
        )
        url_input.fill(updated_value)
        api_key_input.fill("test-api-key")

        authed_page.wait_for_function(
            """
            () => {
              const button = document.querySelector("[data-testid='settings-indexers-prowlarr-save-sync']");
              return !!button && !button.disabled;
            }
            """,
            timeout=5000,
        )
        assert save_button.is_enabled()

    def test_settings_indexers_jackett_save_sync_requires_dirty_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        save_button = authed_page.locator(
            "[data-testid='settings-indexers-jackett-save-sync']"
        ).first
        url_input = authed_page.locator("[data-testid='settings-indexers-jackett-url']").first
        api_key_input = authed_page.locator(
            "[data-testid='settings-indexers-jackett-api-key']"
        ).first

        assert save_button.is_disabled()
        url_input.fill("http://127.0.0.1:9117")
        api_key_input.fill("test-api-key")

        authed_page.wait_for_function(
            """
            () => {
              const button = document.querySelector("[data-testid='settings-indexers-jackett-save-sync']");
              return !!button && !button.disabled;
            }
            """,
            timeout=5000,
        )
        assert save_button.is_enabled()

    def test_settings_clients_add_modal_backdrop_covers_full_viewport(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        authed_page.set_viewport_size({"width": 1440, "height": 1100})
        settings.goto("clients")

        authed_page.locator("[data-testid='settings-clients-add-client']").first.click()

        modal = authed_page.locator("[data-testid='settings-clients-modal']").first
        backdrop = authed_page.locator("[data-testid='settings-clients-modal-backdrop']").first

        modal.wait_for(state="visible", timeout=5000)
        backdrop_box = backdrop.bounding_box()

        assert backdrop_box is not None
        assert backdrop_box["y"] <= 1
        assert backdrop_box["height"] >= 1098

    def test_settings_indexers_add_modal_backdrop_covers_full_viewport(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        authed_page.set_viewport_size({"width": 1440, "height": 1100})
        settings.goto("indexers")

        authed_page.locator("[data-testid='settings-indexers-add-indexer']").first.click()

        modal = authed_page.locator("[data-testid='settings-indexers-modal']").first
        backdrop = authed_page.locator("[data-testid='settings-indexers-modal-backdrop']").first

        modal.wait_for(state="visible", timeout=5000)
        backdrop_box = backdrop.bounding_box()

        assert backdrop_box is not None
        assert backdrop_box["y"] <= 1
        assert backdrop_box["height"] >= 1098

    def test_settings_torznab_add_modal_uses_torznab_field_examples(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        authed_page.locator("[data-testid='settings-indexers-add-indexer']").first.click()
        modal = authed_page.locator("[data-testid='settings-indexers-modal']").first
        modal.wait_for(state="visible", timeout=5000)
        torznab_card = modal.locator("button").filter(has_text="Torznab")
        assert torznab_card.get_by_text("Torrent indexer (1337x, etc.)", exact=True).is_visible()
        torznab_card.click()

        assert modal.locator('input[x-model="form.name"]').get_attribute("placeholder") == "Torznab"
        assert (
            modal.locator('input[x-model="form.url"]').get_attribute("placeholder")
            == "https://api.torznab.com"
        )
        modal.locator('input[x-model="form.name"]').fill("Public Torznab")
        modal.locator('input[x-model="form.url"]').fill("https://indexer.example")
        assert modal.get_by_role("button", name="Add Indexer", exact=True).is_enabled()
        modal.get_by_role("button", name="Advanced Settings", exact=True).click()
        resolver_option = modal.locator("[data-testid='settings-indexers-manual-torznab-resolver']")
        resolver_option.wait_for(state="visible", timeout=5000)
        resolver_toggle = resolver_option.locator('input[x-model="form.resolver_enabled"]')
        assert resolver_option.get_by_text("Ranked browser resolver chain", exact=True).is_visible()
        assert resolver_option.get_by_text(
            "Only available for manually added torznab providers. Pullbox tries ordinary HTTP "
            "first and never sends the API key or search query to a resolver.",
            exact=True,
        ).is_visible()
        assert resolver_toggle.is_disabled()
        assert "toggle-input" in (resolver_toggle.get_attribute("class") or "")

    def test_settings_footer_save_buttons_share_same_height(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)

        selectors = {
            "general": "[data-testid='settings-panel-general'] .settings-footer .btn-primary",
            "media": "[data-testid='settings-panel-media'] .settings-footer .btn-primary",
            "clients": "[data-testid='settings-panel-clients'] .settings-footer .btn-primary",
            "indexers": "[data-testid='settings-panel-indexers'] .settings-footer .btn-primary",
            "ui": "[data-testid='settings-panel-ui'] .settings-footer .btn-primary",
        }

        heights: dict[str, float] = {}
        for tab, selector in selectors.items():
            settings.goto(tab)
            button = authed_page.locator(selector).first
            box = button.bounding_box()
            assert box is not None, f"{tab} save button should be visible"
            heights[tab] = box["height"]

        baseline = heights["general"]
        for tab, height in heights.items():
            assert abs(height - baseline) <= 1.0, (
                f"{tab} save button height {height} does not match general height {baseline}"
            )

    def test_settings_indexers_health_pills_leave_pending_on_page_entry(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("indexers")

        health_pills = authed_page.locator("[data-testid^='settings-indexers-health-']")
        if health_pills.count() == 0:
            pytest.skip("No indexer entries are seeded for this browser run.")

        authed_page.wait_for_function(
            """
            () => {
              const pills = Array.from(document.querySelectorAll("[data-testid^='settings-indexers-health-']"));
              return pills.length > 0 && pills.every((pill) => pill.textContent.trim() !== 'Pending');
            }
            """,
            timeout=5000,
        )

    def test_settings_clients_policy_cards_stack_vertically(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("clients")

        import_card = authed_page.locator("[data-testid='settings-clients-import-card']").first
        failure_card = authed_page.locator("[data-testid='settings-clients-failure-card']").first
        history_card = authed_page.locator("[data-testid='settings-clients-history-card']").first

        import_box = import_card.bounding_box()
        failure_box = failure_card.bounding_box()
        history_box = history_card.bounding_box()

        assert import_box is not None
        assert failure_box is not None
        assert history_box is not None

        assert abs(import_box["x"] - failure_box["x"]) <= 2
        assert abs(import_box["x"] - history_box["x"]) <= 2
        assert failure_box["y"] > import_box["y"] + import_box["height"] - 2
        assert history_box["y"] > failure_box["y"] + failure_box["height"] - 2

    def test_settings_first_cards_align_to_same_top_rail(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        general_card = authed_page.locator(
            "[data-testid='settings-content'] [data-testid='settings-panel-general'] .section-card"
        ).first
        general_box = general_card.bounding_box()
        assert general_box is not None

        for tab in (
            "media",
            "clients",
            "indexers",
            "resolvers",
            "direct",
            "metadata",
            "search",
            "utilities",
            "ui",
        ):
            settings.switch_tab(tab)
            target_card = authed_page.locator(
                f"[data-testid='settings-panel-{tab}'] .section-card"
            ).first
            target_box = target_card.bounding_box()

            assert target_box is not None
            assert abs(target_box["y"] - general_box["y"]) <= 2, (
                f"{tab} top card y={target_box['y']} does not match general y={general_box['y']}"
            )

    def test_direct_download_provider_registration_modal_is_native_and_closable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page_errors: list[str] = []
        authed_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("direct")

        assert authed_page.locator("[data-testid='settings-direct-trust-warning']").is_visible()
        authed_page.locator("[data-testid='settings-direct-add-provider']").click()

        modal = authed_page.locator("[data-testid='settings-direct-modal']")
        authed_page.wait_for_timeout(100)
        component_data = authed_page.locator("[x-data^='directProviderSettings']").get_attribute(
            "x-data"
        )
        assert not page_errors, {"errors": page_errors, "x_data": component_data}
        modal.wait_for(state="visible", timeout=5000)
        assert modal.is_visible()
        assert modal.get_by_label("Provider endpoint").is_visible()
        assert modal.get_by_label("Bearer token").is_visible()
        assert modal.get_by_role("button", name="Register Provider").is_visible()

        modal.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden")

    def test_challenge_resolver_modal_uses_connection_contract_and_fits_viewport(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 900, "height": 720})
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("resolvers")

        authed_page.locator("[data-testid='settings-resolver-add']").click()

        modal = authed_page.locator("[data-testid='settings-resolver-modal']")
        panel = modal.locator(".modal-panel")
        modal.wait_for(state="visible", timeout=5000)
        panel_box = panel.bounding_box()

        assert panel_box is not None
        assert panel_box["x"] > 0
        assert panel_box["y"] > 0
        assert panel_box["x"] + panel_box["width"] <= 900
        assert panel_box["y"] + panel_box["height"] <= 720
        assert abs((panel_box["x"] + (panel_box["width"] / 2)) - 450) <= 2
        assert modal.get_by_test_id("settings-resolver-modal-enabled").is_visible()
        assert modal.get_by_test_id("settings-resolver-modal-private-http").is_visible()
        type_dropdown = modal.get_by_test_id("settings-resolver-type-select")
        type_dropdown.locator("[data-dropdown-select-trigger]").click()
        type_panel = authed_page.get_by_test_id("settings-resolver-type-panel")
        type_panel.wait_for(state="visible", timeout=5000)
        type_panel.locator("[data-dropdown-option][data-value='trawl']").click()
        assert type_dropdown.locator("[data-dropdown-select-input]").input_value() == "trawl"
        assert (
            type_dropdown.locator("[data-dropdown-select-trigger-label]").text_content() == "TRAWL"
        )
        assert modal.get_by_role("button", name="Cancel").is_visible()
        assert modal.get_by_role("button", name="Save Resolver").is_visible()

        modal.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden")

    def test_settings_tab_switch_resets_scroll_to_top(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("clients")

        authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              if (content) {
                content.scrollTop = Math.max(600, content.scrollHeight);
                content.dispatchEvent(new Event("scroll"));
              }
            }
            """
        )
        authed_page.wait_for_function(
            """() => {
                const content = document.getElementById("content");
                return !!content && content.scrollTop > 0;
            }""",
            timeout=5000,
        )

        settings.switch_tab("general")

        authed_page.wait_for_function(
            """() => {
                const content = document.getElementById("content");
                return !!content && content.scrollTop === 0;
            }""",
            timeout=5000,
        )

    def test_settings_save_keeps_active_non_general_tab(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        toggle = authed_page.locator(
            "[data-testid='settings-panel-media'] .settings-row:has(.settings-row-label:text('Normalize Imported Archives to CBZ')) .toggle-switch"
        ).first
        save_button = authed_page.locator(
            "[data-testid='settings-panel-media'] .settings-footer .btn-primary"
        ).first

        assert authed_page.evaluate("() => typeof Alpine !== 'undefined'") is True
        assert save_button.is_disabled()

        toggle.click()
        assert save_button.is_enabled()
        save_button.click()

        authed_page.wait_for_function(
            """
            () => {
              const button = document.querySelector("[data-testid='settings-panel-media'] .settings-footer .btn-primary");
              return !!button && button.disabled;
            }
            """,
            timeout=5000,
        )

        authed_page.wait_for_function(
            "() => window.location.search.includes('tab=media')",
            timeout=5000,
        )
        assert "/settings?tab=media" in authed_page.url
        assert settings.panel("media").is_visible()

    def test_settings_tab_switches_emit_no_page_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        errors: list[str] = []
        console_messages: list[str] = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        authed_page.on(
            "console",
            lambda msg: (
                console_messages.append(msg.text) if msg.type in {"warning", "error"} else None
            ),
        )

        settings = SettingsPage(authed_page, seeded_server)
        settings.goto()

        settings.switch_tab("clients")
        settings.switch_tab("utilities")
        settings.switch_tab("indexers")
        settings.switch_tab("resolvers")
        settings.switch_tab("metadata")
        settings.switch_tab("general")

        assert settings.page_root.is_visible()
        assert not errors
        assert not any("x-collapse" in message for message in console_messages)
        assert not any(
            "Password field is not contained in a form" in message for message in console_messages
        )
        assert not any("Password forms should have" in message for message in console_messages)
        assert not any(
            "Input elements should have autocomplete attributes" in message
            for message in console_messages
        )

    def test_media_toggles_do_not_jump_scroll_position(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 900, "height": 900})
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        format_toggle = authed_page.locator(
            "[data-testid='settings-panel-media'] .settings-row:has(.settings-row-label:text('Normalize Imported Archives to CBZ')) .toggle-switch"
        ).first
        format_toggle.scroll_into_view_if_needed()

        before_scroll = authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              return content ? content.scrollTop : -1;
            }
            """
        )
        assert before_scroll > 0

        format_toggle.click()

        authed_page.wait_for_function(
            """
            (expectedScroll) => {
              const content = document.getElementById("content");
              if (!content) return false;
              const formatToggle = document.querySelector(
                "[data-testid='settings-panel-media'] input[name='convert_to_preferred_format_on_import']"
              );
              const resetButton = formatToggle
                ?.closest("form")
                ?.querySelector(".settings-footer button[type='button']");
              return (
                Math.abs(content.scrollTop - expectedScroll) <= 1 &&
                !!resetButton &&
                window.getComputedStyle(resetButton).display !== "none"
              );
            }
            """,
            arg=before_scroll,
            timeout=5000,
        )

        after_scroll = authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              return content ? content.scrollTop : -1;
            }
            """
        )
        assert abs(after_scroll - before_scroll) <= 1, (
            f"media toggle scrolled content from {before_scroll} to {after_scroll}"
        )

    def test_media_naming_preview_edit_keeps_layout_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 900, "height": 900})
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        authed_page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll("[data-testid='settings-naming-editor'] .settings-media-preview-panel"))
              .every((el) => el.dataset.previewReady === "true")
            """,
            timeout=5000,
        )

        preview = authed_page.locator("#preview-standard").first
        standard_input = authed_page.locator('input[name="comic_file_template"]').first
        standard_input.scroll_into_view_if_needed()

        before = authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              const preview = document.getElementById("preview-standard");
              return {
                scrollTop: content ? content.scrollTop : -1,
                height: preview ? preview.getBoundingClientRect().height : -1,
                text: preview ? preview.textContent || "" : "",
              };
            }
            """
        )
        assert "Loading" not in before["text"]

        def handle_preview(route) -> None:  # type: ignore[no-untyped-def]
            authed_page.wait_for_timeout(350)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "examples": {
                            "comic_file_template": [
                                {
                                    "input": "Batman #1",
                                    "output": "Batman (2016) #001 — revised.cbz",
                                }
                            ]
                        }
                    }
                ),
            )

        authed_page.route("**/api/v1/config/naming/preview", handle_preview)

        standard_input.fill("{Series} ({Year}) #{Issue:03d} — revised")

        authed_page.wait_for_function(
            """
            () => document.getElementById("preview-standard")?.getAttribute("aria-busy") === "true"
            """,
            timeout=5000,
        )

        pending = authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              const preview = document.getElementById("preview-standard");
              return {
                scrollTop: content ? content.scrollTop : -1,
                height: preview ? preview.getBoundingClientRect().height : -1,
                text: preview ? preview.textContent || "" : "",
                busy: preview ? preview.getAttribute("aria-busy") : null,
              };
            }
            """
        )

        assert pending["busy"] == "true"
        assert "Loading" not in pending["text"]
        assert abs(pending["scrollTop"] - before["scrollTop"]) <= 1
        assert abs(pending["height"] - before["height"]) <= 1

        preview.locator("text=revised.cbz").wait_for(state="visible", timeout=5000)

        after = authed_page.evaluate(
            """
            () => {
              const content = document.getElementById("content");
              const preview = document.getElementById("preview-standard");
              return {
                scrollTop: content ? content.scrollTop : -1,
                height: preview ? preview.getBoundingClientRect().height : -1,
                busy: preview ? preview.getAttribute("aria-busy") : null,
              };
            }
            """
        )

        assert after["busy"] == "false"
        assert abs(after["scrollTop"] - before["scrollTop"]) <= 1
        assert abs(after["height"] - before["height"]) <= 1

    def test_settings_media_library_permissions_save_posts_chmod_only_policy(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        requests: list[dict[str, Any]] = []

        def handle_config_update(route) -> None:  # type: ignore[no-untyped-def]
            payload = route.request.post_data_json or {}
            requests.append(payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"updated": sorted(payload.get("values", {}).keys())}),
            )

        authed_page.route("**/api/v1/config", handle_config_update)

        enabled = authed_page.locator(
            "[data-testid='settings-media-library-permissions-enabled']"
        ).first
        folder_mode = authed_page.locator(
            "[data-testid='settings-media-library-folder-mode']"
        ).first
        file_mode = authed_page.locator("[data-testid='settings-media-library-file-mode']").first
        save_button = authed_page.locator("[data-testid='settings-media-import-save']").first

        enabled.evaluate(
            """
            (node) => {
              if (!node.checked) {
                node.checked = true;
                node.dispatchEvent(new Event("input", { bubbles: true }));
                node.dispatchEvent(new Event("change", { bubbles: true }));
              }
            }
            """
        )
        folder_mode.fill("750")
        file_mode.fill("640")

        authed_page.wait_for_function(
            """
            () => {
              const button = document.querySelector("[data-testid='settings-media-import-save']");
              return !!button && !button.disabled;
            }
            """,
            timeout=5000,
        )
        save_button.click()

        authed_page.wait_for_function(
            """
            () => {
              const button = document.querySelector("[data-testid='settings-media-import-save']");
              return !!button && button.disabled;
            }
            """,
            timeout=5000,
        )

        assert requests
        values = requests[-1]["values"]
        assert values["library_permissions_enabled"] == "true"
        assert values["library_permissions_folder_mode"] == "750"
        assert values["library_permissions_file_mode"] == "640"
        assert values["library_permissions_apply_to_created_folders"] in {"true", "false"}
        assert values["library_permissions_apply_to_materialized_files"] in {"true", "false"}
        assert "library_permissions_owner" not in values
        assert "library_permissions_group" not in values

    def test_media_colon_replacement_dropdown_can_extend_past_card(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("media")

        naming_card = (
            authed_page.locator("[data-testid='settings-panel-media'] .section-card")
            .filter(has=authed_page.locator(".section-title-plain:text-is('Naming templates')"))
            .first
        )
        dropdown = authed_page.locator(
            "[data-testid='settings-media-colon-replacement-select']"
        ).first
        trigger = dropdown.locator("[data-dropdown-select-trigger]").first
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first

        trigger.click()
        panel.wait_for(state="visible", timeout=5000)

        card_box = naming_card.bounding_box()
        panel_box = panel.bounding_box()

        assert card_box is not None
        assert panel_box is not None
        assert panel_box["y"] > card_box["y"]
        assert panel_box["y"] + panel_box["height"] > card_box["y"] + card_box["height"], (
            "Colon replacement dropdown should extend beyond the naming templates card "
            "instead of being clipped underneath it."
        )

    def test_settings_shared_dropdowns_update_without_shell_instability(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("ui")

        settings.select_dropdown_option("settings-ui-date-format-select", "YYYY-MM-DD")

        assert settings.page_root.is_visible()
        assert settings.panel("ui").is_visible()
        assert settings.dropdown_value("settings-ui-date-format-select") == "YYYY-MM-DD"
        assert settings.dropdown_label("settings-ui-date-format-select") == "2026-03-22"

        settings.switch_tab("utilities")
        settings.select_dropdown_option("settings-utilities-log-level-select", "ERROR")

        assert settings.page_root.is_visible()
        assert settings.panel("utilities").is_visible()
        assert settings.dropdown_value("settings-utilities-log-level-select") == "ERROR"
        assert settings.dropdown_label("settings-utilities-log-level-select") == "Error"

    def test_settings_search_language_dropdown_selects_and_resets(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        saved_payloads: list[dict[str, Any]] = []

        def handle_config_update(route) -> None:  # type: ignore[no-untyped-def]
            payload = route.request.post_data_json or {}
            saved_payloads.append(payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"updated": sorted(payload.get("values", {}).keys())}),
            )

        authed_page.route("**/api/v1/config", handle_config_update)
        settings.goto("search")

        settings.select_dropdown_option(
            "settings-search-preferred-language-select",
            "es",
        )

        assert settings.dropdown_value("settings-search-preferred-language-select") == "es"
        assert settings.dropdown_label("settings-search-preferred-language-select") == "Spanish"

        modifiers_card = (
            settings.panel("search")
            .locator(".section-card")
            .filter(has=authed_page.get_by_text("Scoring modifiers", exact=True))
            .first
        )
        modifiers_card.get_by_role("button", name="Reset to Defaults").click()

        authed_page.wait_for_function(
            """
            () => document
              .querySelector("[data-testid='settings-search-preferred-language-select']")
              ?.getAttribute("data-dropdown-value") === "en"
            """,
            timeout=5000,
        )
        assert settings.dropdown_value("settings-search-preferred-language-select") == "en"
        assert settings.dropdown_label("settings-search-preferred-language-select") == "English"
        assert saved_payloads[-1]["values"]["preferred_language"] == "en"

    def test_settings_floating_dropdown_preserves_control_metrics(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("ui")

        trigger = (
            settings.dropdown("settings-ui-date-format-select")
            .locator("[data-dropdown-select-trigger]")
            .first
        )
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first

        trigger.click()
        panel.wait_for(state="visible", timeout=5000)

        styles = authed_page.evaluate(
            """
            () => {
              const panel = Array.from(document.querySelectorAll("[data-dropdown-select-panel]"))
                .find((node) => {
                  const style = window.getComputedStyle(node);
                  return style.display !== "none" && style.visibility !== "hidden";
                });
              const option = panel?.querySelector("[data-dropdown-option]");
              const icon = panel?.querySelector(".dropdown-select-option-check");
              if (!panel || !option || !icon) {
                throw new Error("Visible dropdown panel metrics are unavailable");
              }
              const optionStyle = window.getComputedStyle(option);
              const iconStyle = window.getComputedStyle(icon);
              return {
                gap: optionStyle.gap,
                minHeight: optionStyle.minHeight,
                paddingLeft: optionStyle.paddingLeft,
                iconWidth: iconStyle.width,
                iconHeight: iconStyle.height,
              };
            }
            """
        )

        assert styles["gap"] != "normal"
        assert styles["minHeight"] != "0px"
        assert styles["paddingLeft"] != "0px"
        assert styles["iconWidth"] != "auto"
        assert styles["iconHeight"] != "auto"

    def test_settings_ui_theme_choices_fill_card_evenly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("ui")

        theme_buttons = authed_page.locator(
            "[data-testid='settings-panel-ui'] .settings-theme-choice"
        )
        assert theme_buttons.count() == 3

        button_boxes = [theme_buttons.nth(index).bounding_box() for index in range(3)]
        assert all(box is not None for box in button_boxes)
        assert button_boxes[0] is not None
        assert button_boxes[1] is not None
        assert button_boxes[2] is not None

        widths = [box["width"] for box in button_boxes if box is not None]
        assert max(widths) - min(widths) <= 2

        grid_box = (
            authed_page.locator("[data-testid='settings-panel-ui'] .settings-theme-choice")
            .first.locator("xpath=..")
            .bounding_box()
        )
        assert grid_box is not None
        assert abs(button_boxes[0]["x"] - grid_box["x"]) <= 2
        right_edge = button_boxes[2]["x"] + button_boxes[2]["width"]
        grid_right_edge = grid_box["x"] + grid_box["width"]
        assert abs(right_edge - grid_right_edge) <= 2

    def test_settings_utilities_browse_buttons_open_at_effective_configured_paths(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("utilities")

        app_settings = get_settings()
        expected_trash = str(
            resolve_utility_directory(
                db_value="",
                default_parent=app_settings.library_root,
                default_subdir=".trash",
                library_root=app_settings.library_root,
                data_dir=app_settings.data_dir,
            )
        )
        expected_export = str(
            resolve_utility_directory(
                db_value="",
                default_parent=app_settings.data_dir,
                default_subdir="exports",
                library_root=app_settings.library_root,
                data_dir=app_settings.data_dir,
            )
        )

        requested_paths: list[str] = []

        def handle_directories(route) -> None:  # type: ignore[no-untyped-def]
            parsed = urlparse(route.request.url)
            current_path = parse_qs(parsed.query).get("path", [""])[0]
            requested_paths.append(current_path)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": current_path,
                        "parent": "/" if current_path != "/" else None,
                        "directories": [],
                        "quick_links": [],
                    }
                ),
            )

        authed_page.route("**/api/v1/filesystem/directories**", handle_directories)

        authed_page.locator("[data-testid='settings-utilities-trash-folder-browse']").click()
        file_browser = authed_page.locator("[data-testid='file-browser-modal']").first
        file_browser.wait_for(state="visible", timeout=5000)
        assert (
            authed_page.locator("[data-testid='file-browser-title']").first.inner_text()
            == "Select Trash Folder"
        )
        assert file_browser.get_by_text(expected_trash, exact=True).first.is_visible()
        authed_page.locator("[data-testid='file-browser-close']").first.click()
        file_browser.wait_for(state="hidden", timeout=5000)

        authed_page.locator("[data-testid='settings-utilities-export-folder-browse']").click()
        file_browser.wait_for(state="visible", timeout=5000)
        assert (
            authed_page.locator("[data-testid='file-browser-title']").first.inner_text()
            == "Select Export Folder"
        )
        assert file_browser.get_by_text(expected_export, exact=True).first.is_visible()

        assert requested_paths == [expected_trash, expected_export]

    def test_settings_utilities_empty_trash_uses_confirm_modal_and_endpoint(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        settings = SettingsPage(authed_page, seeded_server)
        settings.goto("utilities")

        requests: list[str] = []

        def handle_empty_trash(route) -> None:  # type: ignore[no-untyped-def]
            requests.append(route.request.method)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"message": "Trash emptied.", "deleted_entries": 3}),
            )

        authed_page.route("**/api/v1/utilities/trash/empty", handle_empty_trash)

        authed_page.locator("[data-testid='settings-utilities-empty-trash-now']").click()
        confirm_title = authed_page.locator("#pb-confirm-title")
        confirm_title.wait_for(state="visible", timeout=5000)
        assert confirm_title.is_visible()
        assert authed_page.get_by_text(
            "Empty Utility Trash",
            exact=True,
        ).is_visible()
        assert authed_page.get_by_text(
            "Delete everything currently in the utility trash folder? This cannot be undone.",
            exact=True,
        ).is_visible()
        authed_page.locator("#pb-confirm-dialog").get_by_role(
            "button",
            name="Empty Trash",
            exact=True,
        ).click()

        authed_page.wait_for_timeout(200)
        assert requests == ["POST"]
