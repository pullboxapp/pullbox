"""Unified Import workspace page object."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.pages.base import BasePage

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


class ImportPage(BasePage):
    """Page object for the unified /import workspace and its tabs."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, *, tab: str = "collection", view: str | None = None) -> None:
        """Navigate to the Import workspace, optionally selecting a tab/view."""
        path = f"/import?tab={tab}"
        if view:
            path += f"&view={view}"
        self.navigate(path)
        self.workspace_root.wait_for(state="visible", timeout=5000)

    @property
    def workspace_root(self) -> Locator:
        return self.page.locator("[data-testid='import-page']").first

    @property
    def shell(self) -> Locator:
        return self.page.locator("[data-testid='import-shell']").first

    @property
    def header(self) -> Locator:
        return self.page.locator("[data-testid='import-header']").first

    @property
    def tabs(self) -> Locator:
        return self.page.locator("[data-testid='import-tabs']").first

    def workspace_tab(self, key: str) -> Locator:
        return self.page.locator(f"[data-testid='import-tab-{key}']").first

    @property
    def collection_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-page']").first

    @property
    def source_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-source']").first

    @property
    def source_filesystem_card(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-source-filesystem']").first

    @property
    def source_mylar3_card(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-source-mylar3']").first

    @property
    def source_browse_button(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-source-browse']").first

    @property
    def source_path_input(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-source-path']").first

    @property
    def file_handling_managed(self) -> Locator:
        return self.page.locator("[data-testid='import-file-handling-managed']").first

    @property
    def file_handling_in_place(self) -> Locator:
        return self.page.locator("[data-testid='import-file-handling-in-place']").first

    @property
    def file_handling_in_place_ready(self) -> Locator:
        return self.page.locator("[data-testid='import-file-handling-in-place-ready']").first

    @property
    def file_handling_in_place_blocked(self) -> Locator:
        return self.page.locator("[data-testid='import-file-handling-in-place-blocked']").first

    @property
    def start_scan_button(self) -> Locator:
        return self.page.locator("[data-testid='import-start-scan']").first

    @property
    def source_layout_series_folders(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-series-folders']").first

    @property
    def source_layout_publisher_series(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-publisher-series']").first

    @property
    def source_layout_custom(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-custom']").first

    @property
    def source_layout_custom_fields(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-custom-fields']").first

    @property
    def source_layout_analyze_button(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-analyze']").first

    @property
    def source_layout_fallback_checkbox(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-fallback']").first

    @property
    def source_layout_preview(self) -> Locator:
        return self.page.locator("[data-testid='import-layout-preview']").first

    @property
    def file_browser_modal(self) -> Locator:
        return self.page.locator("[data-testid='file-browser-modal']").first

    @property
    def file_browser_title(self) -> Locator:
        return self.page.locator("[data-testid='file-browser-title']").first

    @property
    def history_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-history-page']").first

    @property
    def unmatched_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-page']").first

    @property
    def stepper(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-stepper']").first

    @property
    def body(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-body']").first

    @property
    def modal_host(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-modal-host']").first

    @property
    def review_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-review']").first

    @property
    def review_selection_summary(self) -> Locator:
        return self.page.locator("[data-import-review-selection-summary]").first

    @property
    def review_import_button(self) -> Locator:
        return self.page.locator("[data-import-review-import-button]").first

    @property
    def review_subtabs(self) -> Locator:
        return self.page.locator("[data-testid='import-review-subtabs']").first

    @property
    def review_series_filters(self) -> Locator:
        return self.page.locator("[data-testid='import-review-series-filters']").first

    @property
    def review_back_button(self) -> Locator:
        return self.page.locator("[data-testid='import-review-back']").first

    @property
    def review_series_tab(self) -> Locator:
        return self.page.locator("[data-testid='import-review-tab-series']").first

    @property
    def review_matched_tab(self) -> Locator:
        return self.page.locator("[data-testid='import-review-series-filter-matched']").first

    def review_sort_button(self, field: str) -> Locator:
        return self.page.locator(f"[data-testid='import-review-sort-{field}']").first

    @property
    def review_rows(self) -> Locator:
        return self.review_panel.locator("table tbody tr").filter(
            has_not=self.review_panel.locator("[data-testid='import-review-no-match-diagnostics']")
        )

    @property
    def review_no_match_details_button(self) -> Locator:
        return self.page.locator("[data-testid='import-review-why-action']").first

    @property
    def review_no_match_diagnostics(self) -> Locator:
        return self.page.locator("[data-testid='import-review-no-match-diagnostics']").first

    @property
    def review_matched_details_button(self) -> Locator:
        return self.page.locator("[data-testid='import-review-matched-why-action']").first

    @property
    def review_matched_diagnostics(self) -> Locator:
        return self.page.locator("[data-testid='import-review-matched-diagnostics']").first

    @property
    def conflicts_tab(self) -> Locator:
        return self.page.locator("[data-testid='import-review-open-conflicts-filter']").first

    @property
    def conflicts_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-conflicts']").first

    @property
    def conflicts_table(self) -> Locator:
        return self.page.locator("[data-testid='import-conflicts-table']").first

    @property
    def save_conflict_choices_button(self) -> Locator:
        return self.page.locator("[data-testid='import-review-save-conflict-choices']").first

    @property
    def reset_conflict_choices_button(self) -> Locator:
        return self.page.locator("[data-testid='import-review-reset-conflict-choices']").first

    @property
    def progress_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-progress']").first

    @property
    def progress_title(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-title']").first

    @property
    def progress_summary(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-summary']").first

    @property
    def progress_phase_label(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-phase-label']").first

    @property
    def progress_phase_detail(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-phase-detail']").first

    @property
    def progress_eta(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-eta']").first

    @property
    def progress_recent_log(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-recent-log']").first

    @property
    def progress_continue_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-continue']").first

    @property
    def progress_continue_results_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-continue-results']").first

    @property
    def progress_view_history_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-view-history']").first

    @property
    def progress_pause_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-pause']").first

    @property
    def progress_resume_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-resume']").first

    @property
    def progress_cancel_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-cancel']").first

    @property
    def progress_cancel_run_button(self) -> Locator:
        return self.page.locator("[data-testid='import-progress-cancel-run']").first

    @property
    def results_panel(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-results']").first

    @property
    def retry_failed_button(self) -> Locator:
        return self.page.locator("[data-testid='import-results-retry-action']").first

    @property
    def rollback_import_button(self) -> Locator:
        return self.page.locator("[data-testid='import-results-rollback-action']").first

    @property
    def search_cv_button(self) -> Locator:
        return self.page.locator(
            "[data-testid='import-review-search-cv-action'], "
            "[data-testid='import-review-change-cv-action']"
        ).first

    @property
    def cv_search_modal(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-cv-search-modal']").first

    @property
    def cv_search_input(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-cv-search-input']").first

    @property
    def cv_search_submit(self) -> Locator:
        return self.page.locator("[data-testid='import-collection-cv-search-submit']").first

    @property
    def series_details_modal_host(self) -> Locator:
        return self.page.locator(
            "[data-testid='import-collection-series-details-modal-host']"
        ).first

    @property
    def series_details_modal(self) -> Locator:
        return self.page.locator("[data-testid='import-series-details-modal']").first

    @property
    def series_details_open_conflicts_button(self) -> Locator:
        return self.page.locator("[data-testid='import-series-details-open-conflicts']").first

    @property
    def history_results(self) -> Locator:
        return self.page.locator("[data-testid='import-history-results']").first

    @property
    def history_toolbar(self) -> Locator:
        return self.page.locator("[data-testid='import-history-toolbar']").first

    @property
    def history_search_input(self) -> Locator:
        return self.page.locator("[data-testid='import-history-search']").first

    @property
    def history_clear_button(self) -> Locator:
        return self.page.locator("[data-testid='import-history-clear']").first

    @property
    def delete_modal(self) -> Locator:
        return self.page.locator("[data-testid='import-history-delete-modal']").first

    @property
    def empty_history_state(self) -> Locator:
        return self.page.locator("[data-testid='import-history-empty']").first

    def job_row(self, job_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-history-job-{job_id}']").first

    def history_sort_button(self, field: str) -> Locator:
        return self.page.locator(f"[data-testid='import-history-sort-{field}']").first

    def log_toggle(self, job_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-history-log-toggle-{job_id}']").first

    def log_panel(self, job_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-history-log-panel-{job_id}']").first

    def resume_button(self, job_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-history-resume-{job_id}']").first

    def delete_button(self, job_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-history-delete-{job_id}']").first

    @property
    def unmatched_view_tabs(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-tabs']").first

    @property
    def unmatched_view_dropdown(self) -> Locator:
        return self.dropdown("import-orphaned-view")

    @property
    def unmatched_results(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-results']").first

    @property
    def unmatched_table(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-table']").first

    @property
    def empty_unmatched_state(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-empty']").first

    @property
    def unmatched_modal_host(self) -> Locator:
        return self.page.locator("[data-testid='import-orphaned-modal-host']").first

    def select_unmatched_view(self, key: str) -> None:
        self.select_dropdown_option("import-orphaned-view", key)

    def show_collection_source_step(self) -> None:
        self.page.evaluate(
            """() => {
                const root = document.querySelector("[data-testid='import-collection-page']");
                if (!root || !window.Alpine) return;
                const data = window.Alpine.$data(root);
                data.step = 1;
                data.jobId = null;
            }"""
        )
        self.source_panel.wait_for(state="visible", timeout=5000)

    def unmatched_row(self, text: str) -> Locator:
        return (
            self.unmatched_results.locator("[data-testid^='import-orphaned-row-']")
            .filter(has_text=text)
            .first
        )

    def search_button(self, row_id: int) -> Locator:
        return self.page.locator(f"[data-testid='import-orphaned-search-{row_id}']").first

    def search_button_for_row(self, text: str) -> Locator:
        return self.unmatched_row(text).locator("[data-testid^='import-orphaned-search-']").first
