"""Focused browser coverage for the Import workspace Follow-up tab."""

from __future__ import annotations

import pytest

from tests.e2e.pages.import_page import ImportPage

pytestmark = pytest.mark.e2e


class TestImportUnmatchedTab:
    """Behavior-first E2E checks for completed-import follow-up."""

    @staticmethod
    def _open_batman_follow_up(import_page: ImportPage) -> None:
        import_page.goto(tab="follow-up")
        import_page.follow_up_jobs_table.wait_for(state="visible", timeout=5000)
        import_page.open_follow_up_job("/tmp/imports/batman-batch")

    def test_import_orphaned_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="follow-up")

        assert import_page.workspace_root.is_visible()
        assert import_page.unmatched_panel.is_visible()
        assert import_page.header.is_visible()
        assert import_page.follow_up_job_list.is_visible()
        assert import_page.follow_up_jobs_table.is_visible()
        import_page.open_follow_up_job("/tmp/imports/batman-batch")
        assert import_page.unmatched_view_tabs.is_visible()
        assert import_page.unmatched_results.is_visible()
        assert import_page.unmatched_table.is_visible()
        assert import_page.unmatched_row("Batman and Robin Eternal").is_visible()

    def test_import_orphaned_tab_switch_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="follow-up")

        import_page.select_unmatched_view("dismissed")
        import_page.wait_for_htmx()

        assert authed_page.locator("[data-testid='import-follow-up-page']").count() == 1
        assert authed_page.locator("[data-testid='import-orphaned-tabs']").count() == 1
        assert authed_page.locator("[data-testid='import-orphaned-results']").count() == 1
        assert import_page.workspace_root.is_visible()
        assert import_page.unmatched_panel.is_visible()
        assert import_page.unmatched_results.is_visible()
        assert import_page.unmatched_row("Batman Universe").is_visible()

    def test_import_orphaned_cv_search_modal_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._open_batman_follow_up(import_page)

        import_page.search_button_for_row("Batman and Robin Eternal").click()
        import_page.wait_for_htmx()

        assert import_page.workspace_root.is_visible()
        assert import_page.unmatched_panel.is_visible()
        assert authed_page.locator("[data-testid='import-header']").count() == 1
        assert authed_page.locator("[data-testid='import-orphaned-results']").count() == 1
        assert authed_page.locator("[data-testid='import-orphaned-cv-search-modal']").is_visible()
        assert authed_page.locator("[data-testid='import-orphaned-cv-search-input']").is_visible()

    def test_import_orphaned_actions_use_icon_buttons_with_tooltips(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._open_batman_follow_up(import_page)

        action_state = authed_page.evaluate(
            """() => {
                const row = Array.from(
                    document.querySelectorAll("[data-testid^='import-orphaned-row-']")
                ).find((el) => (el.textContent || "").includes("Batman and Robin Eternal"));
                if (!row) return null;
                const search = row.querySelector("[data-testid^='import-orphaned-search-']");
                const dismiss = row.querySelector("[data-testid^='import-orphaned-dismiss-']");
                const statusPill = row.querySelector(".pill");
                return {
                    searchTip: search?.getAttribute("data-tip") || "",
                    dismissTip: dismiss?.getAttribute("data-tip") || "",
                    searchText: (search?.textContent || "").trim(),
                    dismissText: (dismiss?.textContent || "").trim(),
                    searchHasSvg: !!search?.querySelector("svg"),
                    dismissHasSvg: !!dismiss?.querySelector("svg"),
                    statusCentered: statusPill?.className.includes("text-center") || false,
                };
            }"""
        )

        assert action_state is not None
        assert action_state["searchTip"] == "Search ComicVine"
        assert action_state["dismissTip"] == "Dismiss"
        assert action_state["searchText"] == ""
        assert action_state["dismissText"] == ""
        assert action_state["searchHasSvg"] is True
        assert action_state["dismissHasSvg"] is True
        assert action_state["statusCentered"] is True

    def test_import_orphaned_recovery_actions_use_icon_buttons_and_modal_hides_root_selector(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._open_batman_follow_up(import_page)

        state = authed_page.evaluate(
            """() => {
                const row = Array.from(
                    document.querySelectorAll("[data-testid^='import-orphaned-row-']")
                ).find((el) => el.querySelector("[data-testid^='import-orphaned-recover-']"));
                if (!row) return null;
                const recover = row.querySelector("[data-testid^='import-orphaned-recover-']");
                return {
                    recoverTip: recover?.getAttribute('data-tip') || '',
                    recoverText: (recover?.textContent || '').trim(),
                    recoverHasSvg: !!recover?.querySelector('svg'),
                };
            }"""
        )

        if state is None:
            pytest.skip("Seeded unmatched data does not include a recovery-pending row.")
        assert state["recoverTip"] == "Continue recovery"
        assert state["recoverText"] == ""
        assert state["recoverHasSvg"] is True

        authed_page.locator("[data-testid^='import-orphaned-recover-']").first.click()
        import_page.wait_for_htmx()

        recovery_modal = authed_page.locator("[data-testid='import-orphaned-recovery-modal']")
        recovery_modal.wait_for(state="visible", timeout=5000)
        assert authed_page.locator("[data-testid='orphaned-recovery-root']").count() == 0
        assert authed_page.locator("[data-dropdown-select-contract='v1']").count() >= 1
        assert authed_page.locator("[data-recovery-skip-toggle]").count() == 0

        skip_button = authed_page.locator("[data-testid^='orphaned-recovery-skip-']").first
        assert skip_button.get_attribute("data-tip") == "Skip this file for now"
        skip_button.click()
        assert skip_button.get_attribute("aria-pressed") == "true"
        assert skip_button.get_attribute("data-tip") == "Restore issue decision"

        issue_trigger = authed_page.locator(
            "[data-testid^='orphaned-recovery-issue-trigger-']"
        ).first
        assert issue_trigger.is_disabled()

    def test_import_orphaned_dismiss_uses_app_confirm_modal_not_browser_dialog(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        browser_dialogs: list[str] = []
        authed_page.on("dialog", lambda dialog: browser_dialogs.append(dialog.message))

        import_page = ImportPage(authed_page, seeded_server)
        self._open_batman_follow_up(import_page)

        dismiss_button = (
            import_page.unmatched_row("Batman and Robin Eternal")
            .locator("[data-testid^='import-orphaned-dismiss-']")
            .first
        )
        dismiss_button.click()

        confirm_modal_title = authed_page.locator("#pb-confirm-title")
        confirm_modal_title.wait_for(state="visible", timeout=5000)

        assert browser_dialogs == []
        assert confirm_modal_title.text_content() == "Dismiss unmatched series"
