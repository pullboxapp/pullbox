"""Focused browser coverage for the Import workspace collection tab."""

from __future__ import annotations

import json
import re
import time

import pytest

from tests.e2e.pages.import_page import ImportPage

pytestmark = pytest.mark.e2e


class TestImportCollectionTab:
    """Behavior-first E2E checks for the Import workspace collection tab."""

    @staticmethod
    def _active_review_job_id(page) -> int | None:  # type: ignore[no-untyped-def]
        return page.evaluate(
            """async () => {
                for (let id = 1; id <= 20; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "review") {
                        return job.id;
                    }
                }

                return null;
            }"""
        )

    @staticmethod
    def _active_matching_job_id(page) -> int | None:  # type: ignore[no-untyped-def]
        return page.evaluate(
            """async () => {
                for (let id = 1; id <= 20; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "matching") {
                        return job.id;
                    }
                }

                return null;
            }"""
        )

    @staticmethod
    def _active_completed_job_id(page) -> int | None:  # type: ignore[no-untyped-def]
        return page.evaluate(
            """async () => {
                for (let id = 1; id <= 20; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "completed") {
                        return job.id;
                    }
                }

                return null;
            }"""
        )

    def _goto_review_step(
        self,
        import_page: ImportPage,
        page,
        base_url: str,
    ) -> int:  # type: ignore[no-untyped-def]
        if page.url == "about:blank":
            page.goto(f"{base_url}/import?tab=history", wait_until="domcontentloaded")
        review_job_id = self._active_review_job_id(page)
        assert review_job_id is not None
        page.goto(
            f"{base_url}/import?tab=collection&resume_job_id={review_job_id}&resume_step=3",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.review_panel.wait_for(state="visible", timeout=5000)
        return review_job_id

    def test_import_collection_renders_stable_shell_without_implicit_resume(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        import_page.show_collection_source_step()

        assert import_page.workspace_root.is_visible()
        assert import_page.collection_panel.is_visible()
        assert import_page.header.is_visible()
        assert import_page.tabs.is_visible()
        assert import_page.stepper.is_visible()
        assert import_page.body.is_visible()
        assert import_page.source_panel.is_visible()
        assert import_page.review_panel.count() == 0
        assert import_page.modal_host.count() == 1

    def test_import_collection_review_tabs_keep_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page_errors: list[str] = []
        authed_page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        import_page.review_matched_details_button.click()
        import_page.review_matched_diagnostics.wait_for(state="visible", timeout=5000)

        assert import_page.workspace_root.is_visible()
        assert import_page.collection_panel.is_visible()
        assert authed_page.locator("[data-testid='import-header']").count() == 1
        assert authed_page.locator("[data-testid='import-collection-stepper']").count() == 1
        assert authed_page.locator("[data-testid='import-collection-body']").count() == 1
        assert import_page.review_matched_diagnostics.is_visible()

        import_page.conflicts_tab.click()
        import_page.wait_for_htmx()
        import_page.conflicts_panel.wait_for(state="visible", timeout=5000)

        assert authed_page.locator("[data-testid='import-header']").count() == 1
        assert authed_page.locator("[data-testid='import-collection-stepper']").count() == 1
        assert authed_page.locator("[data-testid='import-collection-body']").count() == 1
        assert import_page.conflicts_panel.is_visible()
        assert page_errors == []

    def test_import_collection_series_and_matched_tabs_swap_review_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        if import_page.review_matched_tab.count() == 0:
            pytest.skip("Matched tab is unavailable in the seeded review state.")

        assert authed_page.locator("input[name='review_status_filter']").first.input_value() == ""

        import_page.review_matched_tab.click()
        import_page.wait_for_htmx(timeout=10000)
        authed_page.wait_for_function(
            """() => {
                const input = document.querySelector("#import-step-review-shell input[name='review_status_filter']");
                return !!input && input.value === "matched";
            }""",
            timeout=5000,
        )

        import_page.review_series_tab.click()
        import_page.wait_for_htmx(timeout=10000)
        authed_page.wait_for_function(
            """() => {
                const input = document.querySelector("#import-step-review-shell input[name='review_status_filter']");
                return !!input && input.value === "";
            }""",
            timeout=5000,
        )

    def test_import_collection_review_detail_expansion_survives_shell_refresh(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        toggle = authed_page.locator("[data-testid='import-review-matched-why-action']").first
        if toggle.count() == 0:
            pytest.skip("Matched detail rows are unavailable in the seeded review state.")

        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "true"
        authed_page.locator("[data-testid='import-review-matched-diagnostics']").first.wait_for(
            state="visible",
            timeout=5000,
        )

        authed_page.evaluate(
            """async () => {
                const reviewRoot = document.querySelector("[data-testid='import-collection-review']");
                const data = window.Alpine && window.Alpine.$data
                    ? window.Alpine.$data(reviewRoot)
                    : reviewRoot.__x.$data;
                await data.refreshSeriesReview();
            }"""
        )

        refreshed_toggle = authed_page.locator(
            "[data-testid='import-review-matched-why-action']"
        ).first
        assert refreshed_toggle.get_attribute("aria-expanded") == "true"
        authed_page.locator("[data-testid='import-review-matched-diagnostics']").first.wait_for(
            state="visible",
            timeout=5000,
        )

    def test_import_collection_review_series_details_expand_without_page_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page_errors: list[str] = []
        authed_page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        import_page.review_matched_details_button.click()
        import_page.review_matched_diagnostics.wait_for(state="visible", timeout=5000)

        diagnostics_text = import_page.review_matched_diagnostics.text_content() or ""
        assert "Matched files" in diagnostics_text
        assert "CV Issue ID" in diagnostics_text
        assert page_errors == []

    def test_import_collection_conflict_save_reset_round_trip_keeps_table_visible(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        def assert_conflicts_panel_populated() -> None:
            content = (import_page.conflicts_panel.text_content() or "").strip()
            assert content
            has_table = (
                import_page.conflicts_table.count() > 0 and import_page.conflicts_table.is_visible()
            )
            has_empty_state = "No conflicts to resolve." in content
            assert has_table or has_empty_state

        import_page.conflicts_tab.click()
        import_page.wait_for_htmx(timeout=10000)
        import_page.conflicts_panel.wait_for(state="visible", timeout=5000)
        assert_conflicts_panel_populated()

        if (
            import_page.save_conflict_choices_button.count() > 0
            and import_page.save_conflict_choices_button.is_enabled()
        ):
            import_page.save_conflict_choices_button.click()
            import_page.wait_for_htmx(timeout=10000)
            import_page.conflicts_panel.wait_for(state="visible", timeout=5000)
            assert_conflicts_panel_populated()

            if import_page.reset_conflict_choices_button.is_enabled():
                import_page.reset_conflict_choices_button.click()
                import_page.wait_for_htmx(timeout=10000)
                import_page.conflicts_panel.wait_for(state="visible", timeout=5000)
                assert_conflicts_panel_populated()
                if (
                    import_page.conflicts_table.count() > 0
                    and import_page.conflicts_table.is_visible()
                ):
                    assert import_page.save_conflict_choices_button.is_enabled()

        import_page.review_series_tab.click()
        import_page.wait_for_htmx(timeout=10000)
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        import_page.conflicts_tab.click()
        import_page.wait_for_htmx(timeout=10000)
        import_page.conflicts_panel.wait_for(state="visible", timeout=5000)
        assert_conflicts_panel_populated()

    def test_import_collection_review_ignores_stale_saved_selection_storage(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        review_job_id = self._active_review_job_id(authed_page)
        assert review_job_id is not None

        import_page.navigate(f"/import?tab=collection&resume_job_id={review_job_id}&resume_step=3")
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        authed_page.evaluate(
            """([jobId]) => {
                sessionStorage.setItem(
                    `pb-import-review-selection:${jobId}`,
                    JSON.stringify({ reviewToken: "stale-review", ids: [180] }),
                );
            }""",
            [review_job_id],
        )

        authed_page.reload()
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        assert import_page.review_import_button.is_disabled()
        assert (import_page.review_selection_summary.text_content() or "").strip() == (
            "0 items selected for import"
        )

    def test_import_collection_cv_search_modal_opens_without_page_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page_errors: list[str] = []
        authed_page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        review_job_id = self._active_review_job_id(authed_page)
        if review_job_id is None:
            pytest.skip("No active review job is available in the seeded state.")
        import_page.navigate(f"/import?tab=collection&resume_job_id={review_job_id}&resume_step=3")
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        import_page.search_cv_button.click()
        import_page.wait_for_htmx()
        import_page.cv_search_modal.wait_for(state="visible", timeout=5000)
        import_page.cv_search_input.wait_for(state="visible", timeout=5000)

        assert import_page.cv_search_input.input_value().strip()

        import_page.cv_search_submit.click()
        import_page.wait_for_htmx()
        import_page.cv_search_modal.wait_for(state="visible", timeout=5000)

        assert page_errors == []

    def test_import_collection_cv_search_modal_shows_loading_state_on_repeat_search(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        review_job_id = self._active_review_job_id(authed_page)
        if review_job_id is None:
            pytest.skip("No active review job is available in the seeded state.")
        import_page.navigate(f"/import?tab=collection&resume_job_id={review_job_id}&resume_step=3")
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        import_page.search_cv_button.click()
        import_page.wait_for_htmx()
        import_page.cv_search_modal.wait_for(state="visible", timeout=5000)
        import_page.cv_search_input.wait_for(state="visible", timeout=5000)

        route_pattern = re.compile(r".*/import/\d+/series/\d+/cv-search\?q=.*")

        def delayed_route(route) -> None:  # type: ignore[no-untyped-def]
            time.sleep(0.25)
            route.continue_()

        authed_page.route(route_pattern, delayed_route)
        try:
            import_page.cv_search_input.fill("Batman")
            import_page.cv_search_submit.click()
            authed_page.locator(
                "[data-testid='import-collection-cv-search-loading-modal']"
            ).wait_for(state="visible", timeout=5000)
            import_page.cv_search_modal.wait_for(state="visible", timeout=5000)
        finally:
            authed_page.unroute(route_pattern, delayed_route)

    def test_import_collection_cv_override_keeps_origin_view_after_modal_action(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const originalFetch = window.fetch;
                const originalShowToast = window.showToast;
                try {
                    const modal = window.importCvSearchModalData({
                        jobId: 42,
                        seriesId: 7,
                        query: "Persephone",
                    });
                    const events = [];
                    const reviewData = {
                        currentView: "no_match",
                        refreshSeriesReview: function () {
                            events.push("refresh");
                            return Promise.resolve();
                        },
                        openReviewView: function (view) {
                            events.push("open:" + String(view || ""));
                            return Promise.resolve();
                        },
                        refreshReviewSummary: function () {
                            events.push("summary");
                        },
                    };
                    let toast = null;

                    modal.reviewPanelData = function () {
                        return reviewData;
                    };
                    window.fetch = async function () {
                        return {
                            ok: true,
                            json: async function () {
                                return {
                                    id: 99,
                                    status: "matched",
                                    files_conflict: 0,
                                };
                            },
                        };
                    };
                    window.showToast = function (detail) {
                        toast = detail;
                    };

                    modal.selectResult(130322);
                    await new Promise((resolve, reject) => {
                        let attempts = 0;
                        function check() {
                            if (events.length > 0 || toast) {
                                resolve();
                                return;
                            }
                            attempts += 1;
                            if (attempts > 50) {
                                reject(new Error("Timed out waiting for override refresh."));
                                return;
                            }
                            window.setTimeout(check, 10);
                        }
                        check();
                    });

                    return {
                        currentView: reviewData.currentView,
                        events: events,
                        toastMessage: toast && toast.message ? toast.message : "",
                    };
                } finally {
                    window.fetch = originalFetch;
                    window.showToast = originalShowToast;
                }
            }"""
        )

        assert state["currentView"] == "no_match"
        assert state["events"] == ["refresh", "summary"]
        assert "still on your current page" in state["toastMessage"]

    def test_import_collection_cv_override_opens_needs_issue_when_files_remain_unmatched(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const originalFetch = window.fetch;
                const originalShowToast = window.showToast;
                try {
                    const modal = window.importCvSearchModalData({
                        jobId: 42,
                        seriesId: 7,
                        query: "King Dracula",
                    });
                    const events = [];
                    const reviewData = {
                        currentView: "series",
                        refreshSeriesReview: function () {
                            events.push("refresh");
                            return Promise.resolve();
                        },
                        openReviewView: function (view) {
                            events.push("open:" + String(view || ""));
                            this.currentView = view;
                            return Promise.resolve();
                        },
                        refreshReviewSummary: function () {
                            events.push("summary");
                        },
                    };
                    let toast = null;

                    modal.reviewPanelData = function () {
                        return reviewData;
                    };
                    window.fetch = async function () {
                        return {
                            ok: true,
                            json: async function () {
                                return {
                                    id: 99,
                                    status: "no_match",
                                    cv_id: 160000,
                                    files_no_match: 1,
                                    files_conflict: 0,
                                };
                            },
                        };
                    };
                    window.showToast = function (detail) {
                        toast = detail;
                    };

                    modal.selectResult(160000);
                    await new Promise((resolve, reject) => {
                        let attempts = 0;
                        function check() {
                            if (events.length > 0 || toast) {
                                resolve();
                                return;
                            }
                            attempts += 1;
                            if (attempts > 50) {
                                reject(new Error("Timed out waiting for override refresh."));
                                return;
                            }
                            window.setTimeout(check, 10);
                        }
                        check();
                    });

                    return {
                        currentView: reviewData.currentView,
                        events: events,
                        toastMessage: toast && toast.message ? toast.message : "",
                    };
                } finally {
                    window.fetch = originalFetch;
                    window.showToast = originalShowToast;
                }
            }"""
        )

        assert state["currentView"] == "needs_issue"
        assert state["events"] == ["open:needs_issue", "summary"]
        assert "Needs Issue Match" in state["toastMessage"]

    def test_import_collection_cv_override_pending_rematch_keeps_refreshing_review(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const originalFetch = window.fetch;
                const originalShowToast = window.showToast;
                const originalSetTimeout = window.setTimeout;
                try {
                    const modal = window.importCvSearchModalData({
                        jobId: 42,
                        seriesId: 7,
                        query: "Slow Series",
                    });
                    const events = [];
                    const delays = [];
                    function shellFor(counts, rowBuckets) {
                        const shell = document.createElement("div");
                        shell.id = "import-step-review-shell";
                        shell.setAttribute("data-import-review-status-counts", JSON.stringify(counts || {}));
                        if (rowBuckets) {
                            const row = document.createElement("tbody");
                            row.setAttribute("data-import-review-series-row", "99");
                            row.setAttribute("data-import-review-row-buckets", rowBuckets);
                            shell.appendChild(row);
                        }
                        return shell;
                    }
                    const shellRefreshes = [
                        shellFor({ matched: 1 }, ""),
                        shellFor({ needs_issue: 1 }, "needs_issue"),
                    ];
                    const reviewData = {
                        currentView: "series",
                        refreshSeriesReview: function () {
                            events.push("refresh");
                            return Promise.resolve(shellRefreshes.shift() || shellFor({ needs_issue: 1 }, "needs_issue"));
                        },
                        openReviewView: function (view) {
                            events.push("open:" + String(view || ""));
                            this.currentView = view;
                            return Promise.resolve(shellFor({ needs_issue: 1 }, "needs_issue"));
                        },
                        refreshReviewSummary: function () {
                            events.push("summary");
                        },
                    };
                    let toast = null;

                    modal.reviewPanelData = function () {
                        return reviewData;
                    };
                    window.fetch = async function () {
                        return {
                            ok: true,
                            json: async function () {
                                return {
                                    id: 99,
                                    status: "matched",
                                    files_conflict: 0,
                                    diagnostics: { rematch_pending: true },
                                };
                            },
                        };
                    };
                    window.showToast = function (detail) {
                        toast = detail;
                    };
                    window.setTimeout = function (callback, delay) {
                        delays.push(delay);
                        if (delay <= 20) {
                            return originalSetTimeout(callback, delay);
                        }
                        return originalSetTimeout(callback, 0);
                    };

                    modal.selectResult(160000);
                    await new Promise((resolve, reject) => {
                        let attempts = 0;
                        function check() {
                            if (toast && events.includes("open:needs_issue")) {
                                resolve();
                                return;
                            }
                            attempts += 1;
                            if (attempts > 100) {
                                reject(new Error("Timed out waiting for pending needs-issue refresh."));
                                return;
                            }
                            originalSetTimeout(check, 10);
                        }
                        check();
                    });

                    return {
                        events: events,
                        delays: delays,
                        toastMessage: toast && toast.message ? toast.message : "",
                    };
                } finally {
                    window.fetch = originalFetch;
                    window.showToast = originalShowToast;
                    window.setTimeout = originalSetTimeout;
                }
            }"""
        )

        assert state["events"] == ["refresh", "summary", "refresh", "open:needs_issue"]
        assert state["delays"][0] == 500
        assert "rematching the files" in state["toastMessage"]

    def test_import_collection_series_details_conflict_action_no_longer_switches_views(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const originalShowToast = window.showToast;
                try {
                    const modal = window.importSeriesDetailsModalData({
                        jobId: 42,
                        seriesId: 7,
                    });
                    const events = [];
                    let toast = null;

                    window.addEventListener(
                        "import:open-conflicts",
                        function () {
                            events.push("open-conflicts");
                        },
                        { once: true },
                    );
                    window.showToast = function (detail) {
                        toast = detail;
                    };

                    modal.openConflicts();

                    return {
                        open: modal.open,
                        events: events,
                        toastMessage: toast && toast.message ? toast.message : "",
                    };
                } finally {
                    window.showToast = originalShowToast;
                }
            }"""
        )

        assert state["open"] is False
        assert state["events"] == []
        assert "Use the Conflicts tab" in state["toastMessage"]

    def test_import_collection_results_retry_returns_to_execute_flow(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page_errors: list[str] = []
        authed_page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        completed_job_id = authed_page.evaluate(
            """async () => {
                for (let id = 1; id <= 10; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "completed") {
                        return job.id;
                    }
                }

                return null;
            }"""
        )

        assert completed_job_id is not None

        authed_page.evaluate(
            """(jobId) => {
                window.dispatchEvent(new CustomEvent("wizard:advance", {
                    detail: { step: 5, jobId, jobStatus: "completed" },
                }));
            }""",
            completed_job_id,
        )
        import_page.wait_for_htmx()
        import_page.results_panel.wait_for(state="visible", timeout=5000)

        assert import_page.retry_failed_button.is_visible()

        import_page.retry_failed_button.click()
        authed_page.wait_for_function(
            """() => {
                const progress = document.querySelector("[data-testid='import-collection-progress']");
                const results = document.querySelector("[data-testid='import-collection-results']");
                const isVisible = (el) => !!el && el.offsetParent !== null;
                return isVisible(progress) || isVisible(results);
            }""",
            timeout=5000,
        )

        assert page_errors == []

    def test_import_collection_completed_import_requires_explicit_finish_to_show_results(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        completed_job_id = authed_page.evaluate(
            """async () => {
                for (let id = 1; id <= 20; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "completed") {
                        return job.id;
                    }
                }

                return null;
            }""",
        )

        assert completed_job_id is not None

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={completed_job_id}&resume_step=4",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        assert import_page.progress_continue_results_button.is_visible()
        assert import_page.results_panel.count() == 0 or not import_page.results_panel.is_visible()

        import_page.progress_continue_results_button.click()
        import_page.wait_for_htmx(timeout=10000)
        import_page.results_panel.wait_for(state="visible", timeout=5000)

        assert authed_page.locator("[data-testid='page-dock-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-review-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-conflicts-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-collection-footer-dock']").count() == 1

    def test_import_collection_results_places_actions_and_notes_above_detail_cards(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        completed_job_id = authed_page.evaluate(
            """async () => {
                for (let id = 1; id <= 20; id += 1) {
                    const response = await fetch(`/api/v1/import/${id}`);
                    if (!response.ok) {
                        continue;
                    }

                    const job = await response.json();
                    if (job.status === "completed") {
                        return job.id;
                    }
                }

                return null;
            }""",
        )

        assert completed_job_id is not None

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={completed_job_id}&resume_step=4",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)
        import_page.progress_continue_results_button.click()
        import_page.wait_for_htmx(timeout=10000)
        import_page.results_panel.wait_for(state="visible", timeout=5000)

        state = authed_page.evaluate(
            """() => {
                const results = document.querySelector("[data-testid='import-collection-results']");
                const children = Array.from(results.children).filter((el) => el.nodeType === Node.ELEMENT_NODE);
                const sectionCardIndices = children
                    .map((el, index) => el.classList.contains("section-card") ? index : -1)
                    .filter((index) => index >= 0);
                return {
                    actionIndex: children.findIndex((el) => el.dataset.testid === "import-results-action-bar"),
                    notesIndex: children.findIndex((el) => el.dataset.testid === "import-results-follow-up-notes"),
                    sectionCardIndices,
                    leftActions: Array.from(
                        results.querySelectorAll("[data-testid='import-results-action-bar-left'] > *"),
                    ).map((el) => (el.textContent || "").trim()),
                    rightActions: Array.from(
                        results.querySelectorAll("[data-testid='import-results-action-bar-right'] > *"),
                    ).map((el) => (el.textContent || "").trim()),
                    hasImportAnother: (results.textContent || "").includes("Import another collection"),
                    hasResultsSubcopy: !!Array.from(results.querySelectorAll(".section-card .section-body p"))
                        .find((el) => (el.textContent || "").includes("added ·") && (el.textContent || "").includes("failed ·")),
                    hasSourcePill: !!Array.from(results.querySelectorAll(".section-card .section-body *"))
                        .find((el) => (el.textContent || "").includes("Source") && (el.textContent || "").includes("/")),
                    hasOpenConflictsLabel: (results.textContent || "").includes("Open Conflicts"),
                    hasResolvedConflictsCopy: (results.textContent || "").includes("Conflicting file decisions resolved before completion."),
                    hasUnresolvedConflictsCopy: (results.textContent || "").includes("No unresolved file conflicts remained after review."),
                };
            }"""
        )

        assert state["actionIndex"] == 0
        if state["notesIndex"] >= 0:
            assert state["notesIndex"] == 1
            assert state["sectionCardIndices"][0] > state["notesIndex"]
        else:
            assert state["sectionCardIndices"][0] > state["actionIndex"]
        assert "Rollback import" in state["leftActions"][0]
        assert "View import history" in state["leftActions"]
        assert state["rightActions"] == ["View series library"]
        assert state["hasImportAnother"] is False
        assert state["hasResultsSubcopy"] is False
        assert state["hasSourcePill"] is False
        assert state["hasOpenConflictsLabel"] is True
        assert state["hasResolvedConflictsCopy"] is False
        assert state["hasUnresolvedConflictsCopy"] is True

    def test_import_collection_review_back_renders_completed_scan_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        review_job_id = self._active_review_job_id(authed_page)

        assert review_job_id is not None

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={review_job_id}&resume_step=3",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        import_page.review_back_button.click()
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        assert import_page.progress_title.text_content() == "Scan complete"
        assert import_page.progress_phase_label.text_content() == "Complete"
        assert "Preparing to scan" not in (import_page.progress_summary.text_content() or "")
        assert import_page.progress_continue_button.is_visible()
        assert import_page.progress_recent_log.is_visible()
        assert authed_page.locator("[data-testid='page-dock-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-review-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-conflicts-pagination']").count() == 0
        assert authed_page.locator("[data-testid='import-collection-footer-dock']").count() == 1

    def test_import_collection_completed_scan_cancel_discards_review_job(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        review_job_id = self._goto_review_step(import_page, authed_page, seeded_server)

        import_page.review_back_button.click()
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        requests: list[tuple[str, str]] = []

        def intercept_cancel(route) -> None:  # type: ignore[no-untyped-def]
            request = route.request
            requests.append((request.method, request.url))
            if request.method == "DELETE":
                route.fulfill(status=204, body="")
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=f'{{"id": {review_job_id}, "status": "cancelling"}}',
            )

        authed_page.route(f"**/api/v1/import/{review_job_id}*", intercept_cancel)
        authed_page.evaluate("() => { window.pbConfirm = async () => true; }")
        import_page.progress_cancel_button.click()
        authed_page.wait_for_url("**/import", timeout=5000)

        assert requests == [("DELETE", f"{seeded_server}/api/v1/import/{review_job_id}")]

    @pytest.mark.parametrize("terminal_response", ["cancelled", "missing"])
    def test_import_collection_active_cancel_waits_for_terminal_state_before_reset(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        terminal_response: str,
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        state = {"cancel_requested": False, "terminal": False}
        cancel_requests: list[str] = []

        def progress_snapshot(*, terminal: bool) -> dict[str, object]:
            status = "cancelled" if terminal else "matching"
            return {
                "job_id": matching_job_id,
                "status": status,
                "mode": "scan",
                "phase": "done" if terminal else "matching",
                "progress": 100 if terminal else 64,
                "message": (
                    "Import cancelled by user."
                    if terminal
                    else "Finishing the current safe step before cancelling."
                ),
                "control_state": {
                    "can_pause": not state["cancel_requested"],
                    "can_resume": False,
                    "can_cancel": not state["cancel_requested"],
                    "requested_action": "cancel" if state["cancel_requested"] else "none",
                },
            }

        def fulfill_progress_state(route) -> None:  # type: ignore[no-untyped-def]
            if state["terminal"] and terminal_response == "missing":
                route.fulfill(
                    status=404,
                    content_type="application/json",
                    body='{"detail":"Import job not found"}',
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(progress_snapshot(terminal=state["terminal"])),
            )

        def fulfill_cancel_request(route) -> None:  # type: ignore[no-untyped-def]
            cancel_requests.append(route.request.url)
            state["cancel_requested"] = True
            snapshot = progress_snapshot(terminal=False)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "id": matching_job_id,
                        "status": "matching",
                        "progress_snapshot": snapshot,
                    }
                ),
            )

        authed_page.route("**/api/v1/import/*/stream", lambda route: route.abort())
        authed_page.route(
            f"**/import/{matching_job_id}/progress-state",
            fulfill_progress_state,
        )
        authed_page.route(
            f"**/api/v1/import/{matching_job_id}/cancel",
            fulfill_cancel_request,
        )
        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={matching_job_id}&resume_step=2",
            wait_until="domcontentloaded",
        )
        import_page.progress_panel.wait_for(state="visible", timeout=5000)
        import_page.progress_cancel_run_button.wait_for(state="visible", timeout=5000)
        authed_page.evaluate("() => { window.pbConfirm = async () => true; }")

        import_page.progress_cancel_run_button.click()
        authed_page.wait_for_function(
            """() => {
                const button = document.querySelector("[data-testid='import-progress-cancel-run']");
                return !!button && button.disabled && button.textContent.includes("Cancelling");
            }""",
            timeout=5000,
        )

        assert "resume_job_id" in authed_page.url
        assert len(cancel_requests) == 1

        authed_page.evaluate(
            """() => {
                document.querySelector("[data-testid='import-progress-cancel-run']").click();
            }"""
        )
        authed_page.wait_for_timeout(200)
        assert len(cancel_requests) == 1

        state["terminal"] = True
        authed_page.wait_for_url(re.compile(r"/import\?tab=collection$"), timeout=5000)

    def test_import_collection_progress_log_viewer_renders_entries(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        import_page.review_back_button.click()
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        body = authed_page.locator("[data-testid='import-progress-recent-log-body']").first
        body.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_function(
            """() => {
                const body = document.querySelector("[data-testid='import-progress-recent-log-body']");
                return !!body && body.textContent && body.textContent.trim().length > 0 && !body.textContent.includes("Loading...");
            }""",
            timeout=5000,
        )

        body_text = body.text_content() or ""
        assert "No log entries yet." not in body_text
        assert "INFO" in body_text or "WARN" in body_text or "ERROR" in body_text

    def test_import_collection_active_progress_hydrates_without_stream(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        authed_page.route("**/api/v1/import/*/stream", lambda route: route.abort())
        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={matching_job_id}&resume_step=2",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_timeout(3500)

        assert import_page.progress_title.text_content() == "Scan in progress"
        assert "Preparing to scan" not in (import_page.progress_summary.text_content() or "")
        assert (
            import_page.progress_phase_label.text_content()
            == "Matching series against ComicVine..."
        )
        assert (import_page.progress_phase_detail.text_content() or "").strip() != ""
        assert import_page.progress_eta.is_visible()
        assert import_page.progress_recent_log.is_visible()

    def test_import_collection_progress_navigation_does_not_pause_or_resume_runs(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)
        completed_job_id = self._active_completed_job_id(authed_page)

        assert matching_job_id is not None
        assert completed_job_id is not None

        control_requests: list[tuple[str, str]] = []

        def record_control_request(route) -> None:  # type: ignore[no-untyped-def]
            request = route.request
            control_requests.append((request.method, request.url))
            route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":"Unexpected import control request during navigation"}',
            )

        step4_snapshot = {
            "job_id": completed_job_id,
            "status": "importing",
            "mode": "import",
            "phase": "importing",
            "progress": 42,
            "message": "Importing files...",
            "control_state": {
                "can_pause": True,
                "can_resume": False,
                "can_cancel": True,
                "requested_action": "none",
            },
        }
        step4_snapshot_json = json.dumps(step4_snapshot)

        def fulfill_step4_progress_partial(route) -> None:  # type: ignore[no-untyped-def]
            route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""
                <div
                  x-data='importProgressData({completed_job_id}, 5, "filesystem", {step4_snapshot_json}, "import")'
                  x-init="init()"
                  data-testid="import-collection-progress"
                >
                  <h2 data-testid="import-progress-title" x-text="titleText()">Import in progress</h2>
                </div>
                """,
            )

        def fulfill_step4_progress_state(route) -> None:  # type: ignore[no-untyped-def]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=step4_snapshot_json,
            )

        authed_page.route("**/api/v1/import/*/pause", record_control_request)
        authed_page.route("**/api/v1/import/*/resume", record_control_request)
        authed_page.route("**/api/v1/import/*/stream", lambda route: route.abort())
        authed_page.route(
            f"**/import/{completed_job_id}/progress-partial?*mode=import*",
            fulfill_step4_progress_partial,
        )
        authed_page.route(
            f"**/import/{completed_job_id}/progress-state",
            fulfill_step4_progress_state,
        )

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={matching_job_id}&resume_step=2",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        authed_page.locator("[data-testid='sidebar-link-series']").click()
        authed_page.locator("[data-testid='series-page']").wait_for(state="visible", timeout=5000)
        authed_page.wait_for_timeout(500)

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={completed_job_id}&resume_step=4",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        authed_page.locator("[data-testid='sidebar-link-series']").click()
        authed_page.locator("[data-testid='series-page']").wait_for(state="visible", timeout=5000)
        authed_page.wait_for_timeout(500)

        assert control_requests == []

    def test_import_collection_plain_route_restores_active_job_context(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        expected_resume = authed_page.evaluate(
            """() => {
                const link = document.querySelector("[data-testid='import-tab-collection']");
                if (!link) {
                    return null;
                }
                const href = link.getAttribute("href") || "";
                const url = new URL(href, window.location.origin);
                return {
                    href,
                    resumeJobId: url.searchParams.get("resume_job_id"),
                    resumeStep: url.searchParams.get("resume_step"),
                };
            }"""
        )

        assert expected_resume is not None
        assert expected_resume["resumeJobId"] is not None
        assert expected_resume["resumeStep"] is not None

        authed_page.goto(f"{seeded_server}/import?tab=collection", wait_until="domcontentloaded")
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_function(
            """({ jobId, step }) => {
                const url = new URL(window.location.href);
                return (
                    url.searchParams.get("resume_job_id") === String(jobId) &&
                    url.searchParams.get("resume_step") === String(step)
                );
            }""",
            arg={
                "jobId": expected_resume["resumeJobId"],
                "step": expected_resume["resumeStep"],
            },
            timeout=5000,
        )

        if expected_resume["resumeStep"] == "3":
            import_page.review_panel.wait_for(state="visible", timeout=5000)
        else:
            import_page.progress_panel.wait_for(state="visible", timeout=5000)

        assert f"resume_job_id={expected_resume['resumeJobId']}" in authed_page.url
        assert f"resume_step={expected_resume['resumeStep']}" in authed_page.url

    def test_import_collection_pause_swaps_to_resume_without_button_gap(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const originalFetch = window.fetch;
                try {
                    const controller = window.importProgressData(
                        42,
                        5,
                        "filesystem",
                        {
                            job_id: 42,
                            status: "importing",
                            mode: "import",
                            phase: "importing",
                            progress: 64,
                            control_state: {
                                can_pause: true,
                                can_resume: false,
                                can_cancel: true,
                                requested_action: "none",
                            },
                        },
                        "import",
                    );
                    controller.controlState = {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    };
                    controller.pollJobStatus = async function () {
                        return;
                    };
                    window.fetch = async function () {
                        return {
                            ok: true,
                            json: async function () {
                                return {
                                    id: 42,
                                    status: "paused",
                                    progress_snapshot: {
                                        status: "paused",
                                        mode: "import",
                                        phase: "importing",
                                        progress: 64,
                                        message: "Import is paused.",
                                        control_state: {
                                            can_pause: false,
                                            can_resume: true,
                                            can_cancel: true,
                                            requested_action: "none",
                                        },
                                    },
                                };
                            },
                        };
                    };

                    await controller.pauseRun();

                    return {
                        pauseVisible: controller.showPauseAction(),
                        resumeVisible: controller.showResumeAction(),
                        resumeDisabled: controller.isResumeActionDisabled(),
                        cancelVisible: controller.showCancelAction(),
                        optimisticPauseRequested: controller.optimisticPauseRequested,
                        pausing: controller.pausing,
                    };
                } finally {
                    window.fetch = originalFetch;
                }
            }"""
        )

        assert state == {
            "pauseVisible": False,
            "resumeVisible": True,
            "resumeDisabled": False,
            "cancelVisible": True,
            "optimisticPauseRequested": False,
            "pausing": False,
        }

    def test_import_collection_active_import_keeps_pause_visible_when_control_state_is_stale(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        control_state: {
                            can_pause: false,
                            can_resume: true,
                            can_cancel: false,
                            requested_action: "none",
                        },
                    },
                    "import",
                );
                controller.applyJobState({
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 64,
                    control_state: {
                        can_pause: false,
                        can_resume: true,
                        can_cancel: false,
                        requested_action: "none",
                    },
                });

                return {
                    pauseVisible: controller.showPauseAction(),
                    resumeVisible: controller.showResumeAction(),
                    cancelVisible: controller.showCancelAction(),
                    title: controller.titleText(),
                    summary: controller.summaryText(),
                };
            }"""
        )

        assert state == {
            "pauseVisible": True,
            "resumeVisible": False,
            "cancelVisible": True,
            "title": "Import in progress",
            "summary": "Watch the live counters and log output as Pullbox works through the selected series.",
        }

    def test_import_collection_scan_counters_prefer_live_series_stats_over_stale_summary(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const controller = window.importProgressData(
                    42,
                    3,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "scanning",
                        mode: "scan",
                        phase: "scanning",
                        progress: 27,
                        series_found: 27,
                        series_duplicate: 4,
                        series_matched: 9,
                        series_no_match: 2,
                        scan_total_files: 42,
                        total_files_duplicate: 5,
                        total_files_conflict: 3,
                        review_summary: {
                            series_total: 3,
                            series_in_library: 1,
                            series_matched: 2,
                            files_duplicate: 1,
                            series_no_match: 0,
                            files_total: 12,
                            files_conflict: 1,
                        },
                    },
                    "scan",
                );

                controller.applyJobState({
                    status: "scanning",
                    mode: "scan",
                    phase: "scanning",
                    progress: 27,
                    series_found: 27,
                    series_duplicate: 4,
                    series_matched: 9,
                    series_no_match: 2,
                    scan_total_files: 42,
                    total_files_duplicate: 5,
                    total_files_conflict: 3,
                    review_summary: {
                        series_total: 3,
                        series_in_library: 1,
                        series_matched: 2,
                        files_duplicate: 1,
                        series_no_match: 0,
                        files_total: 12,
                        files_conflict: 1,
                    },
                });

                return {
                    seriesTotal: controller.reviewSummary.seriesTotal,
                    inLibrary: controller.reviewSummary.inLibrary,
                    matched: controller.reviewSummary.matched,
                    duplicateCopies: controller.reviewSummary.duplicateCopies,
                    noMatch: controller.reviewSummary.noMatch,
                    filesTotal: controller.reviewSummary.filesTotal,
                    conflicts: controller.reviewSummary.conflicts,
                };
            }"""
        )

        assert state == {
            "seriesTotal": 27,
            "inLibrary": 4,
            "matched": 9,
            "duplicateCopies": 5,
            "noMatch": 2,
            "filesTotal": 42,
            "conflicts": 3,
        }

    def test_import_collection_paused_import_resumes_even_if_control_state_is_stale(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const originalFetch = window.fetch;
                try {
                    const controller = window.importProgressData(
                        42,
                        5,
                        "filesystem",
                        {
                            job_id: 42,
                            status: "paused",
                            mode: "import",
                            phase: "importing",
                            progress: 64,
                            message: "Import is paused.",
                            control_state: {
                                can_pause: false,
                                can_resume: false,
                                can_cancel: true,
                                requested_action: "none",
                            },
                        },
                        "import",
                    );
                    const calls = [];
                    window.fetch = async function (url) {
                        calls.push(String(url));
                        return {
                            ok: true,
                            json: async function () {
                                return {
                                    id: 42,
                                    status: "importing",
                                    progress_snapshot: {
                                        status: "importing",
                                        mode: "import",
                                        phase: "importing",
                                        progress: 64,
                                        message: "Import resume requested.",
                                        control_state: {
                                            can_pause: true,
                                            can_resume: false,
                                            can_cancel: true,
                                            requested_action: "none",
                                        },
                                    },
                                };
                            },
                        };
                    };
                    controller.pollJobStatus = async function () {
                        return;
                    };
                    controller.startPolling = function () {
                        calls.push("startPolling");
                    };
                    controller.startClock = function () {
                        calls.push("startClock");
                    };
                    controller.connectSSE = function () {
                        calls.push("connectSSE");
                    };

                    await controller.resumeRun();

                    return {
                        calls: calls,
                        status: controller.jobStatus,
                        title: controller.titleText(),
                        resumeVisible: controller.showResumeAction(),
                        pauseVisible: controller.showPauseAction(),
                    };
                } finally {
                    window.fetch = originalFetch;
                }
            }"""
        )

        assert state == {
            "calls": [
                "/api/v1/import/42/resume",
                "startClock",
                "startPolling",
                "connectSSE",
            ],
            "status": "importing",
            "title": "Import in progress",
            "resumeVisible": False,
            "pauseVisible": True,
        }

    def test_import_collection_shows_series_item_panel_between_file_checkpoints(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 68,
                    progress_revision: 10,
                    ephemeral_progress: true,
                    current_file_name: "Persephone.2022.Hybrid.Comic.eBook-BitBook.pdf",
                    current_file_stage: "finalizing",
                    current_file_progress_current: 4,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 100,
                    current_file_progress_unit: "steps",
                });

                controller.applyJobState({
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 72,
                    progress_revision: 11,
                    message: "Still fetching ComicVine metadata for 2000AD... Large series can take a few minutes.",
                    current_series_name: "2000AD",
                    current_series: "2000AD",
                    current_file_name: null,
                    current_file_stage: null,
                    current_file_progress_current: null,
                    current_file_progress_total: null,
                    current_file_progress_pct: null,
                    current_file_progress_unit: null,
                    current_item_kind: "series",
                    current_item_stage: "metadata_fetch_wait",
                    current_item_stage_label: "Fetching ComicVine metadata",
                    current_item_progress_pct: 36,
                    control_state: {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    },
                });

                const fetching = {
                    currentSeriesName: controller.currentSeriesName,
                    currentItemName: controller.currentItemName(),
                    currentItemStage: controller.currentItemStageLabel(),
                    currentItemProgress: controller.currentItemProgress(),
                    progress: controller.progress,
                };

                controller.applyJobState({
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 74,
                    progress_revision: 12,
                    message: "Preparing series records for 2000AD...",
                    current_series_name: "2000AD",
                    current_series: "2000AD",
                    current_file_name: null,
                    current_item_kind: "series",
                    current_item_stage: "series_records",
                    current_item_stage_label: "Preparing series records",
                    current_item_progress_pct: 72,
                    control_state: {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    },
                });

                const preparing = {
                    currentSeriesName: controller.currentSeriesName,
                    currentItemName: controller.currentItemName(),
                    currentItemStage: controller.currentItemStageLabel(),
                    currentItemProgress: controller.currentItemProgress(),
                    progress: controller.progress,
                };

                controller.applyJobState({
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 78,
                    progress_revision: 13,
                    message: "Fetching ComicVine metadata for King Dracula (review group 8/26)...",
                    current_series_name: "King Dracula",
                    current_series: "King Dracula",
                    current_file_name: null,
                    current_item_kind: "series",
                    current_item_stage: "metadata_fetch",
                    current_item_stage_label: "Fetching ComicVine metadata",
                    current_item_progress_pct: 8,
                    control_state: {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    },
                });

                const nextSeries = {
                    currentSeriesName: controller.currentSeriesName,
                    currentItemName: controller.currentItemName(),
                    currentItemStage: controller.currentItemStageLabel(),
                    currentItemProgress: controller.currentItemProgress(),
                    currentItemVisible: controller.showCurrentItemProgress(),
                    progress: controller.progress,
                };

                return {
                    currentFileName: controller.currentFileName,
                    currentFileStage: controller.currentFileStage,
                    currentFileProgress: controller.currentFileProgress,
                    fetching,
                    preparing,
                    nextSeries,
                };
            }"""
        )

        assert state == {
            "currentFileName": "",
            "currentFileStage": "",
            "currentFileProgress": 0,
            "fetching": {
                "currentSeriesName": "2000AD",
                "currentItemName": "2000AD",
                "currentItemStage": "Fetching ComicVine metadata",
                "currentItemProgress": 36,
                "progress": 72,
            },
            "preparing": {
                "currentSeriesName": "2000AD",
                "currentItemName": "2000AD",
                "currentItemStage": "Preparing series records",
                "currentItemProgress": 72,
                "progress": 74,
            },
            "nextSeries": {
                "currentSeriesName": "King Dracula",
                "currentItemName": "King Dracula",
                "currentItemStage": "Fetching ComicVine metadata",
                "currentItemProgress": 8,
                "currentItemVisible": True,
                "progress": 78,
            },
        }

    def test_import_collection_progress_formats_elapsed_and_eta_labels(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 50,
                        elapsed_seconds: 75,
                        estimated_seconds_remaining: 125,
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.applyJobState(controller.initialSnapshot);

                return {
                    elapsedLabel: controller.elapsedLabel,
                    etaLabel: controller.etaLabel,
                };
            }"""
        )

        assert state == {
            "elapsedLabel": "Elapsed: 1m 15s",
            "etaLabel": "~2m 5s left",
        }

    def test_import_collection_briefly_holds_completed_fast_file_before_next_file(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 68,
                    progress_revision: 10,
                    ephemeral_progress: true,
                    current_file_name: "Dead Space Salvage.cbz",
                    current_file_stage: "finalizing",
                    current_file_progress_current: 4,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 100,
                    current_file_progress_unit: "steps",
                });

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 69,
                    progress_revision: 11,
                    ephemeral_progress: true,
                    current_file_name: "Fearscape.Vol.01.cbz",
                    current_file_stage: "transferring",
                    current_file_progress_current: 1,
                    current_file_progress_total: 2,
                    current_file_progress_pct: 50,
                    current_file_progress_unit: "steps",
                });

                const immediate = {
                    currentFileName: controller.currentFileName,
                    currentFileStage: controller.currentFileStage,
                    currentFileProgress: controller.currentFileProgress,
                    progress: controller.progress,
                    barTransitionClass: controller.currentFileProgressBarTransitionClass(),
                };

                await new Promise((resolve) => window.setTimeout(resolve, 220));

                const delayed = {
                    currentFileName: controller.currentFileName,
                    currentFileStage: controller.currentFileStage,
                    currentFileProgress: controller.currentFileProgress,
                    progress: controller.progress,
                    barTransitionClass: controller.currentFileProgressBarTransitionClass(),
                };

                return { immediate, delayed };
            }"""
        )

        assert state == {
            "immediate": {
                "currentFileName": "Dead Space Salvage.cbz",
                "currentFileStage": "finalizing",
                "currentFileProgress": 100,
                "progress": 68,
                "barTransitionClass": "transition-none",
            },
            "delayed": {
                "currentFileName": "Fearscape.Vol.01.cbz",
                "currentFileStage": "transferring",
                "currentFileProgress": 50,
                "progress": 69,
                "barTransitionClass": "transition-none",
            },
        }

    def test_import_collection_briefly_holds_completed_file_before_series_event(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 68,
                    progress_revision: 10,
                    ephemeral_progress: true,
                    current_file_name: "Dead Space Salvage.cbz",
                    current_file_stage: "finalizing",
                    current_file_progress_current: 4,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 100,
                    current_file_progress_unit: "steps",
                });

                controller.applyJobState({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 70,
                    progress_revision: 11,
                    message: "Processed 1/4 review groups",
                    current_series_name: "Dead Space Salvage",
                    current_series: "Dead Space Salvage",
                    current_file_name: null,
                    current_file_stage: null,
                    current_file_progress_current: null,
                    current_file_progress_total: null,
                    current_file_progress_pct: null,
                    current_file_progress_unit: null,
                    current_item_kind: "series",
                    current_item_stage: "review_group_complete",
                    current_item_stage_label: "Review group complete",
                    current_item_progress_pct: 100,
                    control_state: {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    },
                });

                const immediate = {
                    currentItemName: controller.currentItemName(),
                    currentItemStage: controller.currentItemStageLabel(),
                    currentItemProgress: controller.currentItemProgress(),
                    currentFileName: controller.currentFileName,
                    progress: controller.progress,
                };

                await new Promise((resolve) => window.setTimeout(resolve, 220));

                const delayed = {
                    currentItemName: controller.currentItemName(),
                    currentItemStage: controller.currentItemStageLabel(),
                    currentItemProgress: controller.currentItemProgress(),
                    currentFileName: controller.currentFileName,
                    progress: controller.progress,
                };

                return { immediate, delayed };
            }"""
        )

        assert state == {
            "immediate": {
                "currentItemName": "Dead Space Salvage.cbz",
                "currentItemStage": "Finalizing imported file",
                "currentItemProgress": 100,
                "currentFileName": "Dead Space Salvage.cbz",
                "progress": 68,
            },
            "delayed": {
                "currentItemName": "Dead Space Salvage",
                "currentItemStage": "Review group complete",
                "currentItemProgress": 100,
                "currentFileName": "",
                "progress": 70,
            },
        }

    def test_import_collection_ignores_stale_durable_file_snapshot_after_live_handoff(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """async () => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        progress_revision: 10,
                        current_file_name: "Dead Space Salvage.cbz",
                        current_file_stage: "finalizing",
                        current_file_progress_current: 4,
                        current_file_progress_total: 4,
                        current_file_progress_pct: 100,
                        current_file_progress_unit: "steps",
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.hydrateFromSnapshot();

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 69,
                    progress_revision: 11,
                    ephemeral_progress: true,
                    current_file_name: "Fearscape.Vol.01.cbz",
                    current_file_stage: "transferring",
                    current_file_progress_current: 1,
                    current_file_progress_total: 2,
                    current_file_progress_pct: 50,
                    current_file_progress_unit: "steps",
                });

                await new Promise((resolve) => window.setTimeout(resolve, 220));

                controller.applyJobState({
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 70,
                    progress_revision: 10,
                    message: "Processing file 1/2 in review group 1/4",
                    current_file_name: "Dead Space Salvage.cbz",
                    current_file_stage: "finalizing",
                    current_file_progress_current: 4,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 100,
                    current_file_progress_unit: "steps",
                    control_state: {
                        can_pause: true,
                        can_resume: false,
                        can_cancel: true,
                        requested_action: "none",
                    },
                });

                return {
                    currentFileName: controller.currentFileName,
                    currentFileStage: controller.currentFileStage,
                    currentFileProgress: controller.currentFileProgress,
                    progress: controller.progress,
                    barTransitionClass: controller.currentFileProgressBarTransitionClass(),
                };
            }"""
        )

        assert state == {
            "currentFileName": "Fearscape.Vol.01.cbz",
            "currentFileStage": "transferring",
            "currentFileProgress": 50,
            "progress": 69,
            "barTransitionClass": "transition-none",
        }

    def test_import_collection_animates_in_file_progress_updates_only(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")

        state = authed_page.evaluate(
            """() => {
                const controller = window.importProgressData(
                    42,
                    5,
                    "filesystem",
                    {
                        job_id: 42,
                        status: "importing",
                        mode: "import",
                        phase: "importing",
                        progress: 64,
                        control_state: {
                            can_pause: true,
                            can_resume: false,
                            can_cancel: true,
                            requested_action: "none",
                        },
                    },
                    "import",
                );

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 66,
                    progress_revision: 10,
                    ephemeral_progress: true,
                    current_file_name: "Persephone.2022.cbz",
                    current_file_stage: "transferring",
                    current_file_progress_current: 1,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 25,
                    current_file_progress_unit: "steps",
                });

                const first = controller.currentFileProgressBarTransitionClass();

                controller.applyEphemeralFileProgress({
                    job_id: 42,
                    status: "importing",
                    mode: "import",
                    phase: "importing",
                    progress: 67,
                    progress_revision: 11,
                    ephemeral_progress: true,
                    current_file_name: "Persephone.2022.cbz",
                    current_file_stage: "transferring",
                    current_file_progress_current: 2,
                    current_file_progress_total: 4,
                    current_file_progress_pct: 50,
                    current_file_progress_unit: "steps",
                });

                return {
                    first,
                    second: controller.currentFileProgressBarTransitionClass(),
                    currentFileProgress: controller.currentFileProgress,
                };
            }"""
        )

        assert state == {
            "first": "transition-none",
            "second": "transition-all duration-300",
            "currentFileProgress": 50,
        }

    def test_import_history_resume_uses_explicit_step_four_for_paused_import(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        url = authed_page.evaluate(
            """() => {
                const page = window.importHistoryPage({});
                return page.buildResumeUrl(42, 4, "paused", "");
            }"""
        )

        assert url == "/import?tab=collection&resume_job_id=42&resume_step=4"

    def test_import_collection_phase_detail_prefers_live_message_over_stale_log(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        authed_page.route("**/api/v1/import/*/stream", lambda route: route.abort())
        authed_page.route(
            f"**/import/{matching_job_id}/progress-state",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="""
                {
                  "status": "matching",
                  "phase": "matching",
                  "progress": 61,
                  "message": "Matching 4/5...",
                  "current_series": "Saga",
                  "estimated_seconds_remaining": 41,
                  "recent_logs": [
                    {
                      "logged_at": "2024-02-02T12:03:00+00:00",
                      "level": "INFO",
                      "message": "Deduplication complete: 3 duplicates found"
                    }
                  ]
                }
                """,
            ),
        )

        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={matching_job_id}&resume_step=2",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_timeout(1500)

        detail = import_page.progress_phase_detail.text_content() or ""
        assert "Matching 4/5..." in detail
        assert "duplicates found" not in detail

    def test_import_collection_prefers_stream_over_continuous_progress_polling(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        poll_count = 0

        def _count_progress_state(route) -> None:  # type: ignore[no-untyped-def]
            nonlocal poll_count
            poll_count += 1
            route.continue_()

        authed_page.route(f"**/import/{matching_job_id}/progress-state", _count_progress_state)
        authed_page.goto(
            f"{seeded_server}/import?tab=collection&resume_job_id={matching_job_id}&resume_step=2",
            wait_until="domcontentloaded",
        )
        import_page.workspace_root.wait_for(state="visible", timeout=5000)
        import_page.progress_panel.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_timeout(3500)

        assert poll_count <= 2

    def test_import_collection_review_controls_keep_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        assert import_page.review_subtabs.is_visible()
        assert import_page.review_back_button.is_visible()

        import_page.review_matched_details_button.click()
        import_page.review_matched_diagnostics.wait_for(state="visible", timeout=5000)

        assert import_page.workspace_root.is_visible()
        assert import_page.review_matched_diagnostics.is_visible()

    def test_import_collection_review_tables_support_shared_sorting_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        self._goto_review_step(import_page, authed_page, seeded_server)

        import_page.review_sort_button("year").click()
        import_page.wait_for_htmx()
        import_page.review_sort_button("year").click()
        import_page.wait_for_htmx()

        first_series_row = import_page.review_panel.locator("table tbody tr").first
        assert "Saga" in (first_series_row.text_content() or "")

    def test_import_collection_source_step_keeps_selection_state_and_opens_browser(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        import_page.show_collection_source_step()

        import_page.source_filesystem_card.click()
        assert import_page.source_filesystem_card.get_attribute("aria-pressed") == "true"
        assert import_page.source_mylar3_card.get_attribute("aria-pressed") == "false"

        import_page.source_browse_button.click()
        import_page.file_browser_modal.wait_for(state="visible", timeout=5000)
        assert import_page.file_browser_title.text_content() == "Browse Directories"

        authed_page.keyboard.press("Escape")
        import_page.file_browser_modal.wait_for(state="hidden", timeout=5000)

        import_page.source_mylar3_card.click()
        assert import_page.source_mylar3_card.get_attribute("aria-pressed") == "true"
        assert import_page.source_filesystem_card.get_attribute("aria-pressed") == "false"

        import_page.source_browse_button.click()
        import_page.file_browser_modal.wait_for(state="visible", timeout=5000)
        assert import_page.file_browser_title.text_content() == "Browse Files"

    def test_import_collection_source_step_previews_selected_layout(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        requests: list[dict[str, object]] = []

        def fulfill_preview(route) -> None:  # type: ignore[no-untyped-def]
            requests.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "effective_spec": {
                            "schema_version": 1,
                            "mode": "preset",
                            "preset": "publisher_series",
                            "series_path_template": None,
                            "issue_filename_template": None,
                            "selected_cluster_id": None,
                            "fallback_to_auto": True,
                        },
                        "classification": "publisher_series",
                        "clusters": [
                            {
                                "cluster_id": "publisher-series",
                                "classification": "publisher_series",
                                "file_count": 2,
                                "directory_count": 2,
                                "confidence": "high",
                                "proposed_series_path_template": "{Publisher}/{Series}",
                                "proposed_issue_filename_template": None,
                                "examples": [
                                    {
                                        "relative_path": "DC Comics/Batman (2011)/Issue 001.cbz",
                                        "publisher": "DC Comics",
                                        "series": "Batman",
                                        "year": 2011,
                                        "issue_number": "1",
                                        "issue_title": None,
                                        "evidence": ["source_layout"],
                                        "warnings": [],
                                    }
                                ],
                            }
                        ],
                        "directories_considered": 2,
                        "files_considered": 2,
                        "files_fitting": 2,
                        "files_ambiguous": 0,
                        "files_outside_root": 0,
                        "archive_probes": 0,
                        "can_keep_in_place": False,
                        "can_apply_future_policy": False,
                        "partial": False,
                        "warnings": [],
                    }
                ),
            )

        authed_page.route("**/api/v1/import/layout-preview", fulfill_preview)
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="collection")
        import_page.show_collection_source_step()

        import_page.source_filesystem_card.click()
        import_page.source_path_input.fill("/imports")
        import_page.source_layout_publisher_series.click()
        import_page.source_layout_analyze_button.click()
        import_page.source_layout_preview.wait_for(state="visible", timeout=5000)

        assert requests[-1] == {
            "source_path": "/imports",
            "source_type": "filesystem",
            "layout": {
                "schema_version": 1,
                "mode": "preset",
                "preset": "publisher_series",
                "fallback_to_auto": True,
            },
        }
        assert "Publisher Series" in (import_page.source_layout_preview.text_content() or "")
        assert "2 of 2 sampled files fit" in (
            import_page.source_layout_preview.text_content() or ""
        )
        assert "DC Comics/Batman (2011)/Issue 001.cbz" in (
            import_page.source_layout_preview.text_content() or ""
        )

        import_page.source_layout_fallback_checkbox.uncheck()
        import_page.source_layout_analyze_button.click()
        import_page.source_layout_preview.wait_for(state="visible", timeout=5000)
        assert requests[-1]["layout"] == {
            "schema_version": 1,
            "mode": "preset",
            "preset": "publisher_series",
            "fallback_to_auto": False,
        }

        import_page.source_layout_custom.click()
        import_page.source_layout_custom_fields.wait_for(state="visible", timeout=5000)
