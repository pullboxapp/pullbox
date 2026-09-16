"""Focused browser coverage for the Import workspace history tab."""

from __future__ import annotations

import pytest

from tests.e2e.pages.import_page import ImportPage

pytestmark = pytest.mark.e2e


class TestImportHistoryTab:
    """Behavior-first E2E checks for the Import workspace history tab."""

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
    def _job_id_for_source_path(
        page,
        source_fragment: str,
    ) -> int | None:  # type: ignore[no-untyped-def]
        return page.evaluate(
            """(fragment) => {
                const rows = Array.from(
                    document.querySelectorAll("[data-testid^='import-history-job-']")
                );
                const match = rows.find((row) => (row.textContent || "").includes(fragment));
                if (!match) {
                    return null;
                }

                const marker = match.getAttribute("data-testid") || "";
                const idMatch = marker.match(/(\\d+)$/);
                return idMatch ? Number(idMatch[1]) : null;
            }""",
            source_fragment,
        )

    def test_import_history_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        assert import_page.workspace_root.is_visible()
        assert import_page.history_panel.is_visible()
        assert import_page.header.is_visible()
        assert import_page.history_results.is_visible()
        assert import_page.history_toolbar.is_visible()
        assert import_page.history_clear_button.is_visible()
        assert import_page.job_row(1).is_visible()
        assert import_page.job_row(2).is_visible()
        assert import_page.job_row(3).is_visible()

    def test_import_history_search_filters_rows_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        import_page.history_search_input.fill("failed-run")
        authed_page.wait_for_timeout(350)
        import_page.wait_for_htmx()

        assert import_page.workspace_root.is_visible()
        assert import_page.history_panel.is_visible()
        assert import_page.history_results.is_visible()
        assert authed_page.locator("[data-testid^='import-history-job-']").count() == 1
        assert import_page.history_results.get_by_text("/tmp/imports/failed-run").is_visible()

    def test_import_history_sorting_reorders_rows_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        import_page.history_sort_button("source_path").click()
        import_page.wait_for_htmx()

        first_row = authed_page.locator("[data-testid^='import-history-job-']").first

        assert import_page.workspace_root.is_visible()
        assert import_page.history_panel.is_visible()
        assert import_page.history_results.is_visible()
        assert "/import?tab=history&sort=-source_path" in authed_page.url
        assert "/tmp/imports/review-queue" in (first_row.text_content() or "")

    def test_import_history_log_panel_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        import_page.log_toggle(1).click()
        import_page.wait_for_htmx()

        assert import_page.workspace_root.is_visible()
        assert import_page.history_panel.is_visible()
        assert authed_page.locator("[data-testid='import-header']").count() == 1
        assert authed_page.locator("[data-testid='import-history-results']").count() == 1

    def test_import_history_log_panel_stays_open_while_live_rows_refresh(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        import_page.log_toggle(matching_job_id).click()
        import_page.wait_for_htmx()
        import_page.log_panel(matching_job_id).locator(
            "[data-log-viewer-contract='v1']"
        ).first.wait_for(
            state="visible",
            timeout=5000,
        )
        authed_page.wait_for_timeout(1400)

        assert (
            import_page.log_panel(matching_job_id)
            .locator("[data-log-viewer-contract='v1']")
            .first.is_visible()
        )
        assert import_page.log_toggle(matching_job_id).get_attribute("aria-expanded") == "true"

    def test_import_history_resume_review_job_returns_review_without_session_marker(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        review_job_id = self._active_review_job_id(authed_page)

        assert review_job_id is not None

        authed_page.evaluate(
            """(jobId) => {
                window.sessionStorage.removeItem(`pb-import-review-advanced:${jobId}`);
            }""",
            review_job_id,
        )

        import_page.resume_button(review_job_id).click()
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        assert (
            import_page.progress_panel.count() == 0 or not import_page.progress_panel.is_visible()
        )
        assert f"resume_job_id={review_job_id}" in authed_page.url
        assert "resume_step=3" in authed_page.url

    def test_import_history_resume_matching_job_returns_progress_view(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        import_page.resume_button(matching_job_id).click()
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        assert f"resume_job_id={matching_job_id}" in authed_page.url
        assert "resume_step=2" in authed_page.url

    def test_import_history_collection_tab_returns_to_active_matching_job(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        matching_job_id = self._active_matching_job_id(authed_page)

        assert matching_job_id is not None

        import_page.workspace_tab("collection").click()
        import_page.progress_panel.wait_for(state="visible", timeout=5000)

        assert f"resume_job_id={matching_job_id}" in authed_page.url
        assert "resume_step=2" in authed_page.url

    def test_import_history_resume_review_job_returns_review_when_session_marker_exists(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        review_job_id = self._active_review_job_id(authed_page)

        assert review_job_id is not None

        authed_page.evaluate(
            """(jobId) => {
                window.sessionStorage.setItem(`pb-import-review-advanced:${jobId}`, "1");
            }""",
            review_job_id,
        )
        import_page.resume_button(review_job_id).click()
        import_page.review_panel.wait_for(state="visible", timeout=5000)

        assert (
            import_page.progress_panel.count() == 0 or not import_page.progress_panel.is_visible()
        )
        assert f"resume_job_id={review_job_id}" in authed_page.url
        assert "resume_step=3" in authed_page.url

    def test_import_history_delete_modal_opens_and_closes_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        failed_job_id = self._job_id_for_source_path(authed_page, "/tmp/imports/failed-run")

        assert failed_job_id is not None

        import_page.delete_button(failed_job_id).click()
        import_page.delete_modal.wait_for(state="visible", timeout=2000)
        authed_page.locator("[data-testid='import-history-delete-cancel']").click()
        import_page.delete_modal.wait_for(state="hidden", timeout=2000)

        assert import_page.workspace_root.is_visible()
        assert import_page.history_panel.is_visible()
        assert authed_page.locator("[data-testid='import-header']").count() == 1
        assert authed_page.locator("[data-testid='import-history-results']").count() == 1

    def test_import_history_delete_optimistically_removes_row_and_refreshes_results_only(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        failed_job_id = self._job_id_for_source_path(authed_page, "/tmp/imports/failed-run")

        assert failed_job_id is not None

        result = authed_page.evaluate(
            """async (jobId) => {
                const root = document.querySelector("[data-testid='import-history-page']");
                if (!root || !window.Alpine) {
                    throw new Error("Import history controller is unavailable");
                }

                const controller = window.Alpine.$data(root);
                const originalFetch = window.fetch.bind(window);
                const originalRefreshPanel = controller.refreshPanel.bind(controller);
                const originalRefreshResults = controller.refreshResults.bind(controller);
                const calls = [];

                window.fetch = async () => ({
                    ok: true,
                    status: 204,
                    json: async () => ({}),
                });
                controller.refreshPanel = function(path) {
                    calls.push({ kind: "panel", path: String(path || "") });
                };
                controller.refreshResults = function(path) {
                    calls.push({ kind: "results", path: String(path || "") });
                };

                try {
                    controller.openDeleteModal(jobId);
                    controller.deleteJob();
                    await new Promise((resolve) => {
                        let attempts = 0;
                        function check() {
                          if (
                            controller.deleting === false &&
                            controller.deleteJobId === null &&
                            calls.length > 0
                          ) {
                            resolve();
                            return;
                          }
                          attempts += 1;
                          if (attempts >= 50) {
                            resolve();
                            return;
                          }
                          window.setTimeout(check, 10);
                        }
                        check();
                    });

                    return {
                        calls,
                        rowStillExists: Boolean(
                            document.querySelector(`[data-testid='import-history-job-${jobId}']`)
                        ),
                        modalOpen: controller.deleteJobId !== null,
                    };
                } finally {
                    window.fetch = originalFetch;
                    controller.refreshPanel = originalRefreshPanel;
                    controller.refreshResults = originalRefreshResults;
                }
            }""",
            failed_job_id,
        )

        assert result["calls"] == [
            {
                "kind": "results",
                "path": "/import?tab=history&sort=-created_at",
            }
        ]
        assert result["rowStillExists"] is False
        assert result["modalOpen"] is False

    def test_import_history_pending_rollback_keeps_row_and_reports_truthfully(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")
        failed_job_id = self._job_id_for_source_path(authed_page, "/tmp/imports/failed-run")

        assert failed_job_id is not None

        result = authed_page.evaluate(
            """async (jobId) => {
                const root = document.querySelector("[data-testid='import-history-page']");
                if (!root || !window.Alpine) {
                    throw new Error("Import history controller is unavailable");
                }

                const controller = window.Alpine.$data(root);
                const originalFetch = window.fetch.bind(window);
                const originalRefreshResults = controller.refreshResults.bind(controller);
                const originalDispatchToast = controller.dispatchToast.bind(controller);
                const calls = [];
                const toasts = [];

                window.fetch = async () => ({
                    ok: true,
                    status: 202,
                    json: async () => ({
                        status: "rollback_pending",
                        message:
                            "Rollback is still stopping an in-progress Story Arc placement. " +
                            "The import remains in history; delete it again after rollback finishes.",
                    }),
                });
                controller.refreshResults = function(path) {
                    calls.push({ kind: "results", path: String(path || "") });
                };
                controller.dispatchToast = function(message, level) {
                    toasts.push({ message: String(message || ""), level: String(level || "") });
                };

                try {
                    controller.openDeleteModal(jobId);
                    controller.deleteJob();
                    await new Promise((resolve) => {
                        let attempts = 0;
                        function check() {
                          if (controller.deleting === false && controller.deleteJobId === null) {
                            resolve();
                            return;
                          }
                          attempts += 1;
                          if (attempts >= 50) {
                            resolve();
                            return;
                          }
                          window.setTimeout(check, 10);
                        }
                        check();
                    });

                    return {
                        calls,
                        toasts,
                        rowStillExists: Boolean(
                            document.querySelector(`[data-testid='import-history-job-${jobId}']`)
                        ),
                        modalOpen: controller.deleteJobId !== null,
                    };
                } finally {
                    window.fetch = originalFetch;
                    controller.refreshResults = originalRefreshResults;
                    controller.dispatchToast = originalDispatchToast;
                }
            }""",
            failed_job_id,
        )

        assert result["calls"] == [
            {
                "kind": "results",
                "path": "/import?tab=history&sort=-created_at",
            }
        ]
        assert result["toasts"] == [
            {
                "message": (
                    "Rollback is still stopping an in-progress Story Arc placement. "
                    "The import remains in history; delete it again after rollback finishes."
                ),
                "level": "info",
            }
        ]
        assert result["rowStillExists"] is True
        assert result["modalOpen"] is False

    @pytest.mark.parametrize(
        "stale_path",
        [
            "/import?tab=collection",
            "/import?tab=collection&resume_job_id=1&resume_step=5",
        ],
    )
    def test_import_history_canonical_url_helpers_ignore_stale_collection_routes(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        stale_path: str,
    ) -> None:
        import_page = ImportPage(authed_page, seeded_server)
        import_page.goto(tab="history")

        result = authed_page.evaluate(
            """(stalePath) => {
                const root = document.querySelector("[data-testid='import-history-page']");
                if (!root || !window.Alpine) {
                    throw new Error("Import history controller is unavailable");
                }

                window.history.replaceState({}, "", stalePath);
                const controller = window.Alpine.$data(root);
                const canonicalPath = controller.historyPath();
                controller.syncBrowserUrl(canonicalPath);
                return {
                    canonicalPath,
                    currentPath: window.location.pathname + window.location.search,
                };
            }""",
            stale_path,
        )

        assert result["canonicalPath"] == "/import?tab=history&sort=-created_at"
        assert result["currentPath"] == "/import?tab=history&sort=-created_at"
        assert import_page.history_panel.is_visible()
        assert import_page.history_results.is_visible()
