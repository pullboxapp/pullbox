"""Focused browser coverage for the rewritten utilities pages."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete

from pullbox.config import get_settings
from pullbox.database import get_session_factory
from pullbox.utilities.models import UtilityJob, UtilityJobItem, UtilityJobLog
from pullbox.utilities.settings import resolve_utility_directory
from tests.e2e.pages.utilities import (
    UtilitiesConverterPage,
    UtilitiesDbCheckPage,
    UtilitiesExportPage,
    UtilitiesIntegrityPage,
    UtilitiesMassConvertPage,
    UtilitiesMassRenamePage,
    UtilitiesPage,
    UtilitiesPermissionsPage,
)

pytestmark = pytest.mark.e2e


def _normalize_font_family(value: str) -> str:
    return value.replace('"', "").replace("'", "")


def _wait_for_animation_frames(page, count: int = 2) -> None:  # type: ignore[no-untyped-def]
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


def _run_async_blocking(coro):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - bubbles to caller
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "exc" in error:
        raise error["exc"]
    return result.get("value")


async def _seed_utility_jobs(
    jobs: list[dict[str, object]],
    logs: list[dict[str, object]] | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(UtilityJobLog))
        await session.execute(delete(UtilityJobItem))
        await session.execute(delete(UtilityJob))
        for job in jobs:
            payload = dict(job)
            payload.pop("progress_pct", None)
            session.add(UtilityJob(**payload))
        for log in logs or []:
            session.add(UtilityJobLog(**log))
        await session.commit()


class TestUtilitiesPage:
    """Behavior-first E2E checks for the utilities shell."""

    def test_utilities_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        assert utilities.page_root.is_visible()
        assert utilities.header.is_visible()
        assert utilities.gauges.is_visible()
        assert utilities.shell.is_visible()
        assert utilities.tabs.is_visible()
        assert utilities.content.is_visible()
        assert utilities.overview_panel.is_visible()
        assert utilities.footer_dock.is_visible()
        assert utilities.converter_card.is_visible()
        assert utilities.tab("utilities").get_attribute("aria-current") == "page"

    def test_utilities_overview_typography_matches_prototype(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        styles = utilities.page.evaluate(
            """
            () => {
              const resolveStyles = (declarations) => {
                const probe = document.createElement('div');
                probe.style.position = 'absolute';
                probe.style.visibility = 'hidden';
                Object.assign(probe.style, declarations);
                document.body.appendChild(probe);
                const computed = window.getComputedStyle(probe);
                const result = {
                  fontFamily: computed.fontFamily,
                  fontSize: computed.fontSize,
                  fontWeight: computed.fontWeight,
                  letterSpacing: computed.letterSpacing,
                  lineHeight: computed.lineHeight,
                  textTransform: computed.textTransform,
                };
                probe.remove();
                return result;
              };

              const subtitle = document.querySelector("[data-testid='utilities-header'] .utilities-header-subtitle");
              const gaugeLabel = document.querySelector("[data-testid='utilities-header'] .utilities-gauge-label");
              const cardCopy = document.querySelector("[data-testid='utilities-overview-card-converter'] .utility-launch-copy");
              const cardTag = document.querySelector("[data-testid='utilities-overview-card-converter'] .utility-launch-tag");

              if (!subtitle || !gaugeLabel || !cardCopy || !cardTag) {
                throw new Error("Utilities overview typography nodes were not found");
              }

              const read = (node) => {
                const computed = window.getComputedStyle(node);
                return {
                  fontFamily: computed.fontFamily,
                  fontSize: computed.fontSize,
                  fontWeight: computed.fontWeight,
                  letterSpacing: computed.letterSpacing,
                  lineHeight: computed.lineHeight,
                  textTransform: computed.textTransform,
                };
              };

              return {
                subtitle: read(subtitle),
                subtitleExpected: resolveStyles({
                  fontFamily: '"DM Sans", sans-serif',
                  fontSize: '0.78rem',
                  fontWeight: '400',
                }),
                gaugeLabel: read(gaugeLabel),
                gaugeLabelExpected: resolveStyles({
                  fontFamily: '"DM Sans", sans-serif',
                  fontSize: '0.52rem',
                  fontWeight: '500',
                  letterSpacing: '0.1em',
                  textTransform: 'uppercase',
                }),
                cardCopy: read(cardCopy),
                cardCopyExpected: resolveStyles({
                  fontFamily: '"DM Sans", sans-serif',
                  fontSize: '0.78rem',
                  fontWeight: '400',
                  lineHeight: '1.5',
                }),
                cardTag: read(cardTag),
                cardTagExpected: resolveStyles({
                  fontFamily: '"JetBrains Mono", monospace',
                  fontSize: '0.6rem',
                  fontWeight: '500',
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                }),
              };
            }
            """
        )

        for key in ("subtitle", "gaugeLabel", "cardCopy", "cardTag"):
            styles[key]["fontFamily"] = _normalize_font_family(styles[key]["fontFamily"])
            styles[f"{key}Expected"]["fontFamily"] = _normalize_font_family(
                styles[f"{key}Expected"]["fontFamily"]
            )

        assert styles["subtitle"] == styles["subtitleExpected"]
        assert styles["gaugeLabel"] == styles["gaugeLabelExpected"]
        assert styles["cardCopy"] == styles["cardCopyExpected"]
        assert styles["cardTag"] == styles["cardTagExpected"]

    def test_utilities_tab_switch_keeps_shell_stable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.switch_tab("queue")

        assert utilities.page_root.is_visible()
        assert authed_page.locator("[data-testid='utilities-page']").count() == 1
        assert authed_page.locator("[data-testid='utilities-shell']").count() == 1
        assert authed_page.locator("[data-testid='utilities-tabs']").count() == 1
        assert authed_page.locator("[data-testid='utilities-content']").count() == 1
        assert utilities.queue_panel.is_visible()
        assert utilities.footer_dock.is_visible()
        assert utilities.tab("queue").get_attribute("aria-current") == "page"
        assert utilities.queue_empty.is_visible()

        utilities.switch_tab("utilities")

        assert utilities.page_root.is_visible()
        assert authed_page.locator("[data-testid='utilities-page']").count() == 1
        assert authed_page.locator("[data-testid='utilities-body']").count() == 1
        assert authed_page.locator("[data-testid='utilities-tabs']").count() == 1
        assert authed_page.locator("[data-testid='utilities-content']").count() == 1
        assert utilities.overview_panel.is_visible()
        assert utilities.footer_dock.is_visible()
        assert utilities.tab("utilities").get_attribute("aria-current") == "page"

    def test_utilities_tab_switch_updates_header_gauges(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        assert utilities.header.get_by_text("BATCH TOOLS", exact=False).is_visible()
        assert utilities.gauges.locator(".utilities-gauge").count() == 2
        assert utilities.gauges.get_by_text("Running", exact=True).is_visible()
        assert utilities.gauges.get_by_text("Queued", exact=True).is_visible()
        assert utilities.gauges.get_by_text("Done", exact=True).count() == 0

        utilities.switch_tab("queue")

        assert utilities.header.get_by_text("JOB QUEUE", exact=False).is_visible()
        assert utilities.gauges.locator(".utilities-gauge").count() == 3
        assert utilities.gauges.get_by_text("Done", exact=True).is_visible()

        utilities.switch_tab("utilities")

        assert utilities.header.get_by_text("BATCH TOOLS", exact=False).is_visible()
        assert utilities.gauges.locator(".utilities-gauge").count() == 2
        assert utilities.gauges.get_by_text("Done", exact=True).count() == 0

    def test_utilities_queue_active_and_queued_rows_use_compact_action_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 1,
                        "queued": 1,
                        "paused": 0,
                        "total_completed": 0,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-log-1",
                                "job_type": "mass_rename",
                                "display_name": "Test Utility Job",
                                "state": "RUNNING",
                                "total_items": 10,
                                "completed_items": 4,
                                "failed_items": 1,
                                "skipped_items": 0,
                                "warning_count": 1,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:05:00Z",
                                "completed_at": None,
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 40.0,
                            },
                            {
                                "id": "job-queued-1",
                                "job_type": "integrity_check",
                                "display_name": "Queued Integrity Scan",
                                "state": "QUEUED",
                                "total_items": 22,
                                "completed_items": 0,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": 0,
                                "created_at": "2026-04-05T08:03:00Z",
                                "started_at": None,
                                "completed_at": None,
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 0.0,
                            },
                        ],
                        "total": 2,
                    }
                ),
            ),
        )
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("queue")

        active_card = utilities.queue_panel.locator(
            "[data-testid='utilities-queue-active-job']"
        ).first
        assert (
            active_card.locator("[data-testid='utilities-queue-active-job-details']").count() == 0
        )
        assert active_card.locator("[data-testid='utilities-queue-active-job-pause']").is_visible()
        assert active_card.locator("[data-testid='utilities-queue-active-job-cancel']").is_visible()
        assert active_card.locator("[data-tip='Job details']").count() == 0

        active_actions = active_card.locator(
            ".utilities-queue-actions .utilities-queue-act-btn:visible"
        )
        assert active_actions.count() == 2
        assert (
            active_card.locator("[data-testid='utilities-queue-active-job-pause'] svg path").count()
            >= 1
        )
        assert (
            active_card.locator(
                "[data-testid='utilities-queue-active-job-cancel'] svg path"
            ).count()
            >= 1
        )

        queued_card = utilities.queue_panel.locator(
            "[data-testid='utilities-queue-queued-section'] tbody"
        ).first
        assert queued_card.locator("[data-tip='Job details']").count() == 0
        queued_actions = queued_card.locator(".utilities-queue-actions .utilities-queue-act-btn")
        assert queued_actions.count() == 1
        assert queued_card.locator("[aria-label='Cancel job']").is_visible()

    def test_utilities_history_log_viewer_omits_refresh_action(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _run_async_blocking(
            _seed_utility_jobs(
                [
                    {
                        "id": "job-log-2",
                        "job_type": "mass_convert_pipeline",
                        "display_name": "Completed Utility Job",
                        "state": "COMPLETED",
                        "total_items": 4,
                        "completed_items": 4,
                        "failed_items": 0,
                        "skipped_items": 0,
                        "warning_count": 0,
                        "queue_position": None,
                        "created_at": "2026-04-05T08:00:00Z",
                        "started_at": "2026-04-05T08:05:00Z",
                        "completed_at": "2026-04-05T08:10:00Z",
                        "created_by": "admin",
                        "error_message": None,
                        "parent_job_id": None,
                    }
                ],
                [
                    {
                        "job_id": "job-log-2",
                        "item_id": None,
                        "timestamp": "2026-04-05T08:10:00Z",
                        "level": "INFO",
                        "message": "Conversion completed successfully",
                        "extra": "{}",
                        "file_path": (
                            "/Users/adam/Downloads/extremely/long/path/that/should/"
                            "stay/inside/the/history/log/viewer/container/when/the/"
                            "details/row/is/expanded/output.cbz"
                        ),
                    }
                ],
            )
        )
        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 0,
                        "queued": 0,
                        "paused": 0,
                        "total_completed": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-log-2",
                                "job_type": "mass_convert_pipeline",
                                "display_name": "Completed Utility Job",
                                "state": "COMPLETED",
                                "total_items": 4,
                                "completed_items": 4,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:05:00Z",
                                "completed_at": "2026-04-05T08:10:00Z",
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 100.0,
                            }
                        ],
                        "total": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs/job-log-2/logs**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "entries": [
                            {
                                "id": 2,
                                "job_id": "job-log-2",
                                "item_id": None,
                                "timestamp": "2026-04-05T08:10:00Z",
                                "level": "INFO",
                                "message": "Conversion completed successfully",
                                "extra": "{}",
                                "file_path": (
                                    "/Users/adam/Downloads/extremely/long/path/that/should/"
                                    "stay/inside/the/history/log/viewer/container/when/the/"
                                    "details/row/is/expanded/output.cbz"
                                ),
                            }
                        ],
                        "total_count": 1,
                    }
                ),
            ),
        )

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")

        utilities.history_panel.locator("[data-tip='Job details']").first.click()

        viewer = utilities.history_panel.locator("[data-log-viewer-contract='v1']").first
        viewer.wait_for(state="visible", timeout=5000)

        assert viewer.locator("[data-tip='Download']").first.is_visible()
        assert viewer.locator("[data-tip='Refresh']").count() == 0
        assert viewer.locator("[data-tip='Close']").first.is_visible()

        viewer.locator(".log-line").first.click()
        history_table = utilities.history_table
        table_box = history_table.bounding_box()
        viewer_box = viewer.bounding_box()
        assert table_box is not None
        assert viewer_box is not None
        assert viewer_box["width"] <= table_box["width"] + 2
        assert viewer.locator("[data-tip='Download']").first.is_visible()
        assert viewer.locator("[data-tip='Close']").first.is_visible()

    def test_utilities_export_field_cards_render_as_single_line_rows(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        export_page = UtilitiesExportPage(authed_page, seeded_server)
        export_page.goto()

        first_group = export_page.page.locator(
            "[data-testid='utilities-export-field-grid'] .utility-export-group-card"
        ).first
        first_card = first_group.locator("label.utility-step-card").first

        metrics = first_card.evaluate(
            """
            (node) => {
              const label = node.querySelector('.utility-export-field-label');
              const source = node.querySelector('.utility-export-field-sub');
              const meta = node.querySelector('.utility-export-field-meta');
              if (!label || !source || !meta) {
                throw new Error('Export field row nodes were not found');
              }
              const labelRect = label.getBoundingClientRect();
              const sourceRect = source.getBoundingClientRect();
              return {
                display: window.getComputedStyle(node).display,
                metaDisplay: window.getComputedStyle(meta).display,
                sameLine: Math.abs(labelRect.top - sourceRect.top) < 4,
                paddingTop: parseFloat(window.getComputedStyle(node).paddingTop),
                paddingBottom: parseFloat(window.getComputedStyle(node).paddingBottom),
              };
            }
            """
        )
        assert metrics["display"] == "block"
        assert metrics["metaDisplay"] == "flex"
        assert metrics["sameLine"] is True
        assert metrics["paddingTop"] == metrics["paddingBottom"]
        assert metrics["paddingTop"] <= 10

    def test_utility_tool_cards_match_utilities_color_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        export_page = UtilitiesExportPage(authed_page, seeded_server)
        export_page.goto()

        styles = export_page.page.evaluate(
            """
            () => {
              const resolveStyles = (declarations) => {
                const probe = document.createElement('div');
                probe.style.position = 'absolute';
                probe.style.visibility = 'hidden';
                probe.style.borderStyle = 'solid';
                probe.style.borderWidth = '1px';
                Object.assign(probe.style, declarations);
                document.body.appendChild(probe);
                const computed = window.getComputedStyle(probe);
                const result = {
                  backgroundColor: computed.backgroundColor,
                  borderColor: computed.borderColor,
                  boxShadow: computed.boxShadow,
                };
                probe.remove();
                return result;
              };

              const headerCard = document.querySelector("[data-testid='utilities-export-card']");
              const workspaceCard = document.querySelector(
                "[data-testid='utilities-export-page'] .utility-workspace-shell"
              );
              const insetPanel = document.querySelector(
                "[data-testid='utilities-export-json-options']"
              );

              if (!headerCard || !workspaceCard || !insetPanel) {
                throw new Error("Utilities export card contract nodes were not found");
              }

              const headerComputed = window.getComputedStyle(headerCard);
              const workspaceComputed = window.getComputedStyle(workspaceCard);
              const insetComputed = window.getComputedStyle(insetPanel);

              return {
                expectedOuter: resolveStyles({
                  backgroundColor: "var(--pb-surface-card)",
                  borderColor: "var(--pb-border-subtle)",
                  boxShadow: "var(--pb-shadow-1)",
                }),
                expectedInset: resolveStyles({
                  backgroundColor: "var(--pb-surface-app)",
                  borderColor: "var(--pb-border-subtle)",
                }),
                header: {
                  backgroundColor: headerComputed.backgroundColor,
                  borderColor: headerComputed.borderColor,
                  boxShadow: headerComputed.boxShadow,
                },
                workspace: {
                  backgroundColor: workspaceComputed.backgroundColor,
                  borderColor: workspaceComputed.borderColor,
                  boxShadow: workspaceComputed.boxShadow,
                },
                inset: {
                  backgroundColor: insetComputed.backgroundColor,
                  borderColor: insetComputed.borderColor,
                },
              };
            }
            """
        )

        assert styles["header"]["backgroundColor"] == styles["expectedOuter"]["backgroundColor"]
        assert styles["header"]["borderColor"] == styles["expectedOuter"]["borderColor"]
        assert styles["header"]["boxShadow"] == styles["expectedOuter"]["boxShadow"]

        assert styles["workspace"]["backgroundColor"] == styles["expectedOuter"]["backgroundColor"]
        assert styles["workspace"]["borderColor"] == styles["expectedOuter"]["borderColor"]
        assert styles["workspace"]["boxShadow"] == styles["expectedOuter"]["boxShadow"]

        assert styles["inset"]["backgroundColor"] == styles["expectedInset"]["backgroundColor"]
        assert styles["inset"]["borderColor"] == styles["expectedInset"]["borderColor"]

    def test_permissions_card_navigates_to_workflow_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/utilities/permissions/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "scope": "folder",
                        "item_count": 0,
                        "folder_count": 0,
                        "file_count": 0,
                        "items": [],
                    }
                ),
            ),
        )
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.permissions_card.click()
        authed_page.wait_for_url("**/utilities/permissions", timeout=5000)

        page = UtilitiesPermissionsPage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.card.is_visible()
        assert page.footer_dock.is_visible()
        assert page.run_mode_select.input_value() == "dry_run"
        assert page.scope_select.input_value() == "folder"
        assert page.folder_mode_input.input_value() == "755"
        assert page.file_mode_input.input_value() == "644"
        assert page.browse_folder_button.is_visible()
        assert page.browse_files_button.is_hidden()
        assert page.preview_table.is_visible()
        assert page.folder_count.inner_text() == "0"
        assert page.file_count.inner_text() == "0"
        assert page.start_button.is_disabled()
        assert "recursive library permissions" in page.header.inner_text().lower()
        footer_text = page.footer_dock.inner_text().lower()
        assert "mode" in footer_text
        assert "dry-run" in footer_text
        assert "scope" in footer_text
        assert "select folders" in footer_text

    def test_permissions_folder_scope_preview_uses_recursive_counts(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        request_payloads: list[dict[str, object]] = []

        def fulfill_preview(route) -> None:  # type: ignore[no-untyped-def]
            payload = route.request.post_data_json or {}
            request_payloads.append(payload)
            if payload.get("scope") != "folder":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "scope": "library",
                            "item_count": 0,
                            "folder_count": 0,
                            "file_count": 0,
                            "items": [],
                        }
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "scope": "folder",
                        "item_count": 4,
                        "folder_count": 2,
                        "file_count": 2,
                        "items": [
                            {
                                "file_path": "/library/Batman (2016)",
                                "name": "Batman (2016)",
                                "item_type": "folder",
                                "target_mode": "755",
                            },
                            {
                                "file_path": "/library/Batman (2016)/Annuals",
                                "name": "Annuals",
                                "item_type": "folder",
                                "target_mode": "755",
                            },
                            {
                                "file_path": "/library/Batman (2016)/Batman 001.cbz",
                                "name": "Batman 001.cbz",
                                "item_type": "file",
                                "target_mode": "644",
                            },
                        ],
                    }
                ),
            )

        authed_page.route("**/api/v1/utilities/permissions/preview", fulfill_preview)

        page = UtilitiesPermissionsPage(authed_page, seeded_server)
        page.goto()
        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-permissions-page']");
              const data = window.Alpine.$data(root);
              data.setScope("folder");
              data.applySelectedFolders({
                mode: "directories",
                directories: [{ path: "/library/Batman (2016)", name: "Batman (2016)" }]
              });
            }
            """
        )

        page.preview_table.get_by_text("Batman 001.cbz", exact=True).wait_for(
            state="visible",
            timeout=5000,
        )

        assert page.folder_count.inner_text() == "2"
        assert page.file_count.inner_text() == "2"
        assert request_payloads[-1]["scope"] == "folder"
        assert request_payloads[-1]["file_paths"] == ["/library/Batman (2016)"]

    def test_permissions_validation_requires_selected_targets(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesPermissionsPage(authed_page, seeded_server)
        page.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-permissions-page']");
              const data = window.Alpine.$data(root);
              data.applySelectedFolders({
                mode: "directories",
                directories: [{ path: "/library/Batman (2016)", name: "Batman (2016)" }]
              });
              data.includeFolders = false;
              data.includeFiles = false;
              data.validationError = "";
              data.submitting = false;
            }
            """
        )

        page.start_button.click()

        expect(page.error_panel).to_contain_text("Choose files, folders, or both.")

    def test_permissions_apply_requires_confirmation_before_submit(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesPermissionsPage(authed_page, seeded_server)
        page.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-permissions-page']");
              const data = window.Alpine.$data(root);
              data.applySelectedFolders({
                mode: "directories",
                directories: [{ path: "/library/Batman (2016)", name: "Batman (2016)" }]
              });
              data.runMode = "apply";
              data.confirmApply = false;
              data.validationError = "";
              data.submitting = false;
            }
            """
        )

        page.start_button.click()

        expect(page.error_panel).to_contain_text("Confirm that the dry-run output was reviewed")

    def test_permissions_submit_posts_seed_safe_chmod_job_config(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesPermissionsPage(authed_page, seeded_server)
        page.goto()

        authed_page.route(
            "**/api/v1/utilities/jobs",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"job_id":"permissions-job-1"}',
            ),
        )
        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-permissions-page']");
              const data = window.Alpine.$data(root);
              data.runMode = "apply";
              data.scope = "folder";
              data.selectedFolders = [
                { path: "/tmp/pullbox-e2e-library/01-batman", name: "01-batman" }
              ];
              data.folderMode = "750";
              data.fileMode = "640";
              data.includeFolders = true;
              data.includeFiles = true;
              data.confirmApply = true;
              data.validationError = "";
              data.submitting = false;
            }
            """
        )

        with authed_page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/api/v1/utilities/jobs" in response.url
            )
        ) as create_response:
            page.start_button.click()

        payload = create_response.value.request.post_data_json or {}
        assert payload["job_type"] == "library_permissions"
        assert payload["display_name"] == "Library Permissions — Apply"
        assert payload["config"] == {
            "scope": "paths",
            "run_mode": "apply",
            "folder_mode": "750",
            "file_mode": "640",
            "include_folders": True,
            "include_files": True,
            "file_paths": ["/tmp/pullbox-e2e-library/01-batman"],
            "confirm_apply": True,
        }

    def test_utilities_log_download_uses_json_attachment(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        tmp_path,
    ) -> None:
        _run_async_blocking(
            _seed_utility_jobs(
                [
                    {
                        "id": "job-log-download",
                        "job_type": "mass_convert_pipeline",
                        "display_name": "Downloadable Utility Job",
                        "state": "COMPLETED",
                        "total_items": 2,
                        "completed_items": 2,
                        "failed_items": 0,
                        "skipped_items": 0,
                        "warning_count": 0,
                        "queue_position": None,
                        "created_at": "2026-04-05T08:00:00Z",
                        "started_at": "2026-04-05T08:05:00Z",
                        "completed_at": "2026-04-05T08:10:00Z",
                        "created_by": "admin",
                        "error_message": None,
                        "parent_job_id": None,
                    }
                ],
                [
                    {
                        "job_id": "job-log-download",
                        "item_id": None,
                        "timestamp": "2026-04-05T08:10:00Z",
                        "level": "INFO",
                        "message": "Finished export",
                        "extra": "{}",
                        "file_path": "/tmp/output.cbz",
                    }
                ],
            )
        )
        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 0,
                        "queued": 0,
                        "paused": 0,
                        "total_completed": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-log-download",
                                "job_type": "mass_convert_pipeline",
                                "display_name": "Downloadable Utility Job",
                                "state": "COMPLETED",
                                "total_items": 2,
                                "completed_items": 2,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:05:00Z",
                                "completed_at": "2026-04-05T08:10:00Z",
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 100.0,
                            }
                        ],
                        "total": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs/job-log-download/logs**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "entries": [
                            {
                                "id": 3,
                                "job_id": "job-log-download",
                                "item_id": None,
                                "timestamp": "2026-04-05T08:10:00Z",
                                "level": "INFO",
                                "message": "Finished export",
                                "extra": "{}",
                                "file_path": "/tmp/output.cbz",
                            }
                        ],
                        "total_count": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs/job-log-download/logs/download*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="utility_job_20260405_080000.json"'
                },
                body=json.dumps(
                    {
                        "job": {"id": "job-log-download"},
                        "filters": {"level": None, "search": None},
                        "total_count": 1,
                        "entries": [{"message": "Finished export"}],
                    },
                    indent=2,
                )
                + "\n",
            ),
        )

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")

        utilities.history_panel.locator("[data-tip='Job details']").first.click()
        viewer = utilities.history_panel.locator("[data-log-viewer-contract='v1']").first
        viewer.wait_for(state="visible", timeout=5000)
        assert viewer.locator("[data-tip='Download']").first.get_attribute("hx-boost") == "false"

        with authed_page.expect_download() as download_info:
            viewer.locator("[data-tip='Download']").first.click()

        download = download_info.value
        assert download.suggested_filename == "utility_job_20260405_080000.json"
        target = tmp_path / download.suggested_filename
        download.save_as(target)
        body = target.read_text()
        assert body.startswith("{\n  ")
        assert '\n  "job": {' in body
        authed_page.wait_for_function(
            """
            () => {
              const content = document.getElementById('content');
              if (!content) {
                return false;
              }
              return (
                content.getAttribute('data-page-swap-phase') === null &&
                window.getComputedStyle(content).pointerEvents !== 'none'
              );
            }
            """
        )
        assert viewer.is_visible()

    def test_utilities_history_log_viewer_pauses_background_polling_while_open(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _run_async_blocking(
            _seed_utility_jobs(
                [
                    {
                        "id": "job-log-1",
                        "job_type": "mass_rename",
                        "display_name": "Test Utility Job",
                        "state": "COMPLETED",
                        "total_items": 10,
                        "completed_items": 10,
                        "failed_items": 1,
                        "skipped_items": 0,
                        "warning_count": 1,
                        "queue_position": None,
                        "created_at": "2026-04-05T08:00:00Z",
                        "started_at": "2026-04-05T08:05:00Z",
                        "completed_at": "2026-04-05T08:10:00Z",
                        "created_by": "admin",
                        "error_message": None,
                        "parent_job_id": None,
                    }
                ],
                [
                    {
                        "job_id": "job-log-1",
                        "item_id": None,
                        "timestamp": "2026-04-05T08:10:00Z",
                        "level": "WARNING",
                        "message": "Rename preview skipped one file",
                        "extra": '{"path": "/tmp/test.cbz"}',
                        "file_path": "/tmp/test.cbz",
                    }
                ],
            )
        )
        queue_calls = 0
        jobs_calls = 0
        log_calls = 0

        def fulfill_queue(route) -> None:  # type: ignore[no-untyped-def]
            nonlocal queue_calls
            queue_calls += 1
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 1,
                        "queued": 0,
                        "paused": 0,
                        "total_completed": 1,
                    }
                ),
            )

        def fulfill_jobs(route) -> None:  # type: ignore[no-untyped-def]
            nonlocal jobs_calls
            jobs_calls += 1
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-log-1",
                                "job_type": "mass_rename",
                                "display_name": "Test Utility Job",
                                "state": "COMPLETED",
                                "total_items": 10,
                                "completed_items": 10,
                                "failed_items": 1,
                                "skipped_items": 0,
                                "warning_count": 1,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:05:00Z",
                                "completed_at": "2026-04-05T08:10:00Z",
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 100.0,
                            }
                        ],
                        "total": 1,
                    }
                ),
            )

        def fulfill_logs(route) -> None:  # type: ignore[no-untyped-def]
            nonlocal log_calls
            log_calls += 1
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "entries": [
                            {
                                "id": 1,
                                "job_id": "job-log-1",
                                "item_id": None,
                                "timestamp": "2026-04-05T08:10:00Z",
                                "level": "WARNING",
                                "message": "Rename preview skipped one file",
                                "extra": '{"path": "/tmp/test.cbz"}',
                                "file_path": "/tmp/test.cbz",
                            }
                        ],
                        "total_count": 1,
                    }
                ),
            )

        authed_page.route("**/api/v1/utilities/queue", fulfill_queue)
        authed_page.route("**/api/v1/utilities/jobs?limit=50", fulfill_jobs)
        authed_page.route("**/api/v1/utilities/jobs/job-log-1/logs**", fulfill_logs)

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")
        authed_page.evaluate(
            """
            () => {
              const originalSetInterval = window.setInterval.bind(window);
              const originalClearInterval = window.clearInterval.bind(window);
              window.__pbUtilitiesTestIntervals = [];
              window.setInterval = function (fn, delay, ...args) {
                const id = window.__pbUtilitiesTestIntervals.length + 1;
                window.__pbUtilitiesTestIntervals.push({ id, fn, delay, args, cleared: false });
                return id;
              };
              window.clearInterval = function (id) {
                const entry = (window.__pbUtilitiesTestIntervals || []).find((item) => item.id === id);
                if (entry) {
                  entry.cleared = true;
                  return;
                }
                return originalClearInterval(id);
              };

              const root = document.querySelector("[data-testid='utilities-queue-panel']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities queue Alpine state not found");
              }
              if (data._pollInterval) {
                  originalClearInterval(data._pollInterval);
                  data._pollInterval = null;
              }
              data.startPolling();
            }
            """.replace(
                "[data-testid='utilities-queue-panel']",
                "[data-testid='utilities-history-panel']",
            )
        )

        utilities.history_panel.locator("[data-tip='Job details']").first.click()
        viewer = utilities.history_panel.locator("[data-log-viewer-contract='v1']").first
        viewer.wait_for(state="visible", timeout=5000)
        viewer.get_by_text("Rename preview skipped one file", exact=False).first.wait_for(
            state="visible",
            timeout=5000,
        )
        assert viewer.locator(".log-badge").first.inner_text().strip() == "WARN"

        baseline_queue_calls = queue_calls
        baseline_jobs_calls = jobs_calls
        baseline_log_calls = log_calls

        viewer.locator("button").filter(has_text="warn").first.click()
        _wait_for_animation_frames(authed_page)

        assert log_calls == baseline_log_calls
        authed_page.evaluate(
            """
            () => {
              for (const entry of window.__pbUtilitiesTestIntervals || []) {
                if (entry.delay === 5000 && !entry.cleared) {
                  entry.fn(...entry.args);
                }
              }
            }
            """
        )

        assert queue_calls == baseline_queue_calls
        assert jobs_calls == baseline_jobs_calls
        assert log_calls == baseline_log_calls

    def test_utilities_queue_paused_job_shows_resume_control(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _run_async_blocking(_seed_utility_jobs([]))
        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 0,
                        "queued": 0,
                        "paused": 1,
                        "total_completed": 0,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-paused-1",
                                "job_type": "mass_rename",
                                "display_name": "Paused Utility Job",
                                "state": "PAUSED",
                                "total_items": 10,
                                "completed_items": 4,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:05:00Z",
                                "completed_at": None,
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 40.0,
                            }
                        ],
                        "total": 1,
                    }
                ),
            ),
        )

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("queue")

        utilities.queue_panel.locator("[data-testid='utilities-queue-active-job']").filter(
            has_text="Paused Utility Job"
        ).first.wait_for(state="visible", timeout=5000)
        active_card = utilities.queue_panel.locator(
            "[data-testid='utilities-queue-active-job']"
        ).first
        assert active_card.locator("[data-testid='utilities-queue-active-job-resume']").is_visible()
        assert active_card.locator("[data-testid='utilities-queue-active-job-pause']").is_hidden()

    def test_utilities_queue_progress_bars_use_series_bar_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _run_async_blocking(_seed_utility_jobs([]))
        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 1,
                        "queued": 1,
                        "paused": 0,
                        "total_completed": 2,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-running-1",
                                "job_type": "mass_convert_pipeline",
                                "display_name": "Running Convert Batch",
                                "state": "RUNNING",
                                "total_items": 10,
                                "completed_items": 4,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": None,
                                "created_at": "2026-04-05T08:00:00Z",
                                "started_at": "2026-04-05T08:02:00Z",
                                "completed_at": None,
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 40.0,
                            },
                            {
                                "id": "job-queued-1",
                                "job_type": "integrity_check",
                                "display_name": "Queued Integrity Scan",
                                "state": "QUEUED",
                                "total_items": 22,
                                "completed_items": 0,
                                "failed_items": 0,
                                "skipped_items": 0,
                                "warning_count": 0,
                                "queue_position": 0,
                                "created_at": "2026-04-05T08:03:00Z",
                                "started_at": None,
                                "completed_at": None,
                                "created_by": "admin",
                                "error_message": None,
                                "parent_job_id": None,
                                "progress_pct": 0.0,
                            },
                        ],
                        "total": 2,
                    }
                ),
            ),
        )

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("queue")
        utilities.queue_panel.locator("[data-testid='utilities-queue-active-job']").filter(
            has_text="Running Convert Batch"
        ).first.wait_for(state="visible", timeout=5000)

        active_row = (
            utilities.queue_panel.locator("[data-testid='utilities-queue-active-section'] tbody")
            .first.locator("tr")
            .first
        )
        active_items_cell = active_row.locator("td").nth(2)
        active_progress_cell = active_row.locator("td").nth(3)
        active_elapsed_cell = active_row.locator("td").nth(4)
        active_track = active_progress_cell.locator(".series-mission-control-bar-track").first
        active_fill = active_progress_cell.locator(".series-mission-control-bar-fill").first
        active_pct = active_progress_cell.locator(".series-mission-control-bar-pct").first

        assert active_track.is_visible()
        assert active_fill.is_visible()
        assert active_pct.text_content() == "40%"

        active_items_box = active_items_cell.bounding_box()
        active_progress_box = active_progress_cell.bounding_box()
        active_elapsed_box = active_elapsed_cell.bounding_box()
        active_track_box = active_track.bounding_box()
        active_fill_box = active_fill.bounding_box()
        assert active_items_box is not None
        assert active_progress_box is not None
        assert active_elapsed_box is not None
        assert active_track_box is not None
        assert active_fill_box is not None
        assert (
            abs(
                (active_items_box["y"] + active_items_box["height"] / 2)
                - (active_progress_box["y"] + active_progress_box["height"] / 2)
            )
            < 4
        )
        assert (
            abs(
                (active_elapsed_box["y"] + active_elapsed_box["height"] / 2)
                - (active_progress_box["y"] + active_progress_box["height"] / 2)
            )
            < 4
        )
        assert active_fill_box["width"] > active_track_box["width"] * 0.2

        queued_row = (
            utilities.queue_panel.locator("[data-testid='utilities-queue-queued-section'] tbody")
            .first.locator("tr")
            .first
        )
        queued_items_cell = queued_row.locator("td").nth(2)
        queued_progress_cell = queued_row.locator("td").nth(3)
        queued_elapsed_cell = queued_row.locator("td").nth(4)
        queued_track = queued_progress_cell.locator(".series-mission-control-bar-track").first
        queued_pct = queued_progress_cell.locator(".series-mission-control-bar-pct").first

        assert queued_track.is_visible()
        assert queued_pct.text_content() == "—"

        queued_items_box = queued_items_cell.bounding_box()
        queued_progress_box = queued_progress_cell.bounding_box()
        queued_elapsed_box = queued_elapsed_cell.bounding_box()
        assert queued_items_box is not None
        assert queued_progress_box is not None
        assert queued_elapsed_box is not None
        assert (
            abs(
                (queued_items_box["y"] + queued_items_box["height"] / 2)
                - (queued_progress_box["y"] + queued_progress_box["height"] / 2)
            )
            < 4
        )
        assert (
            abs(
                (queued_elapsed_box["y"] + queued_elapsed_box["height"] / 2)
                - (queued_progress_box["y"] + queued_progress_box["height"] / 2)
            )
            < 4
        )

    def test_utilities_history_table_supports_sorting_and_status_filter(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        history_jobs = [
            {
                "id": "job-alpha",
                "job_type": "file_convert",
                "display_name": "Alpha Convert",
                "state": "COMPLETED",
                "total_items": 4,
                "completed_items": 4,
                "failed_items": 0,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:00:00Z",
                "started_at": "2026-04-05T08:01:00Z",
                "completed_at": "2026-04-05T08:15:00Z",
                "created_by": "admin",
                "error_message": None,
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
            {
                "id": "job-beta",
                "job_type": "integrity_check",
                "display_name": "Beta Partial Review",
                "state": "COMPLETED",
                "total_items": 5,
                "completed_items": 4,
                "failed_items": 1,
                "skipped_items": 0,
                "warning_count": 1,
                "queue_position": None,
                "created_at": "2026-04-05T08:20:00Z",
                "started_at": "2026-04-05T08:21:00Z",
                "completed_at": "2026-04-05T08:25:00Z",
                "created_by": "admin",
                "error_message": None,
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
            {
                "id": "job-zulu",
                "job_type": "mass_rename",
                "display_name": "Zulu Failure",
                "state": "FAILED",
                "total_items": 3,
                "completed_items": 1,
                "failed_items": 2,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:30:00Z",
                "started_at": "2026-04-05T08:31:00Z",
                "completed_at": "2026-04-05T08:35:00Z",
                "created_by": "admin",
                "error_message": "Rename failed",
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
        ]
        _run_async_blocking(_seed_utility_jobs(history_jobs))

        authed_page.route(
            "**/api/v1/utilities/queue",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "running": 0,
                        "queued": 0,
                        "paused": 0,
                        "total_completed": 3,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/utilities/jobs?limit=50",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"jobs": history_jobs, "total": len(history_jobs)}),
            ),
        )

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")

        assert utilities.history_table.is_visible()
        assert utilities.history_status_filter.is_visible()
        assert utilities.history_clear_button.is_visible()

        partial_row = utilities.history_table.locator("tbody").filter(
            has_text="Beta Partial Review"
        )
        status_text = (partial_row.locator("td").nth(1).text_content() or "").replace("\n", "")
        status_text = "".join(status_text.split())
        assert status_text == "!Partial"

        alpha_row = utilities.history_table.locator("tbody").filter(has_text="Alpha Convert").first
        alpha_primary_row = alpha_row.locator("tr").first
        completed_cell = alpha_primary_row.locator("td").nth(3)
        actions_cell = alpha_primary_row.locator("td").nth(4)
        completed_box = completed_cell.bounding_box()
        actions_box = actions_cell.bounding_box()
        assert completed_box is not None
        assert actions_box is not None
        assert completed_box["x"] + completed_box["width"] <= actions_box["x"] + 1

        utilities.select_history_status("partial")
        filtered_rows = utilities.history_table.locator("tbody")
        assert filtered_rows.count() == 1
        first_filtered_title = (
            filtered_rows.first.locator("td").first.locator("span").first.text_content() or ""
        ).strip()
        assert first_filtered_title == "Beta Partial Review"

        utilities.select_history_status("")
        authed_page.wait_for_function(
            """
            () => document.querySelectorAll("[data-testid='utilities-history-table'] tbody")
              .length === 3
            """,
            timeout=5000,
        )

        authed_page.locator("[data-testid='utilities-history-sort-job']").click()
        utilities.wait_for_htmx()
        authed_page.wait_for_function(
            """
            () => (
              (document.querySelector("[data-testid='utilities-history-table'] tbody td span")?.textContent || "")
                .trim()
                .startsWith("Zulu Failure")
            )
            """,
            timeout=5000,
        )
        authed_page.wait_for_function(
            """
            () => (
              document.querySelector("[data-testid='utilities-history-sort-job']")
                ?.getAttribute("hx-get") || ""
            ).includes("sort=job")
            """,
            timeout=5000,
        )
        assert (
            (
                utilities.history_table.locator("tbody").first.locator("td").first.text_content()
                or ""
            )
            .strip()
            .startswith("Zulu Failure")
        )

        authed_page.locator("[data-testid='utilities-history-sort-job']").click()
        utilities.wait_for_htmx()
        authed_page.wait_for_function(
            """
            () => (
              (document.querySelector("[data-testid='utilities-history-table'] tbody td span")?.textContent || "")
                .trim()
                .startsWith("Alpha Convert")
            )
            """,
            timeout=5000,
        )
        assert (
            (
                utilities.history_table.locator("tbody").first.locator("td").first.text_content()
                or ""
            )
            .strip()
            .startswith("Alpha Convert")
        )

    def test_utilities_history_clear_button_removes_terminal_jobs(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        current_jobs = [
            {
                "id": "job-running",
                "job_type": "integrity_check",
                "display_name": "Live Integrity Job",
                "state": "RUNNING",
                "total_items": 10,
                "completed_items": 3,
                "failed_items": 0,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T09:00:00Z",
                "started_at": "2026-04-05T09:01:00Z",
                "completed_at": None,
                "created_by": "admin",
                "error_message": None,
                "parent_job_id": None,
                "progress_pct": 30.0,
            },
            {
                "id": "job-history-1",
                "job_type": "file_convert",
                "display_name": "Completed Utility Job",
                "state": "COMPLETED",
                "total_items": 2,
                "completed_items": 2,
                "failed_items": 0,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:00:00Z",
                "started_at": "2026-04-05T08:01:00Z",
                "completed_at": "2026-04-05T08:02:00Z",
                "created_by": "admin",
                "error_message": None,
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
            {
                "id": "job-history-2",
                "job_type": "mass_rename",
                "display_name": "Cancelled Utility Job",
                "state": "CANCELLED",
                "total_items": 3,
                "completed_items": 1,
                "failed_items": 0,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:10:00Z",
                "started_at": "2026-04-05T08:11:00Z",
                "completed_at": "2026-04-05T08:12:00Z",
                "created_by": "admin",
                "error_message": "Cancelled by user",
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
        ]
        _run_async_blocking(_seed_utility_jobs(current_jobs))

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")

        assert utilities.history_clear_button.is_visible()
        utilities.history_clear_button.click()

        confirm_dialog = authed_page.locator("#pb-confirm-dialog")
        confirm_dialog.locator("button", has_text="Clear History").click()
        utilities.history_clear_button.wait_for(state="hidden", timeout=5000)

        assert not utilities.history_clear_button.is_visible()
        utilities.switch_tab("queue")
        active_job_title = (
            utilities.queue_panel.locator("[data-testid='utilities-queue-active-job']")
            .first.locator("div.text-sm.font-medium.text-pb-text")
            .first.text_content()
            or ""
        ).strip()
        assert active_job_title == "Live Integrity Job"

    def test_utilities_history_completed_job_can_queue_rollback(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        current_jobs = [
            {
                "id": "job-history-rollback-parent",
                "job_type": "mass_convert_pipeline",
                "display_name": "Completed Convert Job",
                "state": "COMPLETED",
                "total_items": 3,
                "completed_items": 3,
                "failed_items": 0,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:00:00Z",
                "started_at": "2026-04-05T08:01:00Z",
                "completed_at": "2026-04-05T08:05:00Z",
                "created_by": "admin",
                "error_message": None,
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
            {
                "id": "job-history-failed",
                "job_type": "mass_rename",
                "display_name": "Failed Rename Job",
                "state": "FAILED",
                "total_items": 2,
                "completed_items": 0,
                "failed_items": 2,
                "skipped_items": 0,
                "warning_count": 0,
                "queue_position": None,
                "created_at": "2026-04-05T08:10:00Z",
                "started_at": "2026-04-05T08:11:00Z",
                "completed_at": "2026-04-05T08:12:00Z",
                "created_by": "admin",
                "error_message": "Rename failed",
                "parent_job_id": None,
                "progress_pct": 100.0,
            },
        ]
        _run_async_blocking(_seed_utility_jobs(current_jobs))

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto("history")

        completed_row = utilities.history_table.locator("tbody").filter(
            has_text="Completed Convert Job"
        )
        failed_row = utilities.history_table.locator("tbody").filter(has_text="Failed Rename Job")

        completed_rollback = completed_row.locator(
            "[data-testid='utilities-history-rollback']"
        ).first
        failed_rollback = failed_row.locator("[data-testid='utilities-history-rollback']").first

        assert completed_rollback.is_visible()
        assert failed_rollback.is_hidden()

        completed_rollback.click()
        confirm_dialog = authed_page.locator("#pb-confirm-dialog")
        confirm_dialog.locator("button", has_text="Queue Rollback").click()

        authed_page.wait_for_function(
            """() => {
                const nodes = Array.from(
                    document.querySelectorAll(
                        "[data-testid='utilities-history-table'] [data-testid='utilities-history-rollback']"
                    )
                );
                return nodes.filter((node) => {
                    const style = window.getComputedStyle(node);
                    return (
                        style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        node.getClientRects().length > 0
                    );
                }).length === 0;
            }""",
            timeout=5000,
        )
        rollback_job = authed_page.evaluate(
            """async () => {
                const response = await fetch('/api/v1/utilities/jobs?limit=50');
                const data = await response.json();
                return (data.jobs || []).find(
                    (job) => job.display_name === 'Rollback: Completed Convert Job'
                ) || null;
            }"""
        )
        assert rollback_job is not None
        assert rollback_job["parent_job_id"] == "job-history-rollback-parent"

    def test_utilities_converter_renders_stable_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        assert converter.page_root.is_visible()
        assert converter.header.is_visible()
        assert converter.workspace.is_visible()
        assert converter.card.is_visible()
        assert converter.back_link.is_visible()
        assert converter.footer_dock.is_visible()
        assert converter.source_format.is_visible()
        assert converter.start_button.is_visible()

    def test_utilities_converter_matches_prototype_shell_copy_and_structure(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        assert converter.header.get_by_text("FILE CONVERTER", exact=False).is_visible()
        assert converter.header.get_by_text(
            "Single-file CBR, CB7, PDF → CBZ", exact=True
        ).is_visible()
        assert converter.header.get_by_text("One-off", exact=True).is_visible()

        root = converter.page_root
        assert root.get_by_text("Conversion Setup", exact=True).is_visible()
        assert root.get_by_text("Files to Convert", exact=True).is_visible()
        assert root.get_by_text("Browse", exact=True).is_visible()
        assert root.get_by_text("Preview output", exact=True).count() == 0
        assert root.get_by_role("link", name="Cancel", exact=True).is_visible()
        assert root.get_by_text("Start conversion", exact=True).is_visible()
        assert not root.get_by_text("Single-file repair", exact=True).is_visible()
        assert not root.get_by_text("Selection", exact=True).is_visible()
        assert not root.get_by_text("Target format", exact=True).is_visible()

    def test_utilities_converter_typography_matches_prototype(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        styles = converter.page.evaluate(
            """
            () => {
              const resolveStyles = (declarations) => {
                const probe = document.createElement('div');
                probe.style.position = 'absolute';
                probe.style.visibility = 'hidden';
                Object.assign(probe.style, declarations);
                document.body.appendChild(probe);
                const computed = window.getComputedStyle(probe);
                const result = {
                  fontFamily: computed.fontFamily,
                  fontSize: computed.fontSize,
                  fontWeight: computed.fontWeight,
                  letterSpacing: computed.letterSpacing,
                  textTransform: computed.textTransform,
                  lineHeight: computed.lineHeight,
                };
                probe.remove();
                return result;
              };

              const sectionLabel = document.querySelector(".utility-tool-section-label");
              const fieldLabel = document.querySelector(".utility-tool-field-label");
              const browseButton = document.querySelector("[data-testid='utilities-converter-browse-files']");
              const actionFooter = document.querySelector(".utility-tool-action-footer");

              if (!sectionLabel || !fieldLabel || !browseButton || !actionFooter) {
                throw new Error("Utilities converter prototype nodes were not found");
              }

              const read = (node) => {
                const computed = window.getComputedStyle(node);
                return {
                  fontFamily: computed.fontFamily,
                  fontSize: computed.fontSize,
                  fontWeight: computed.fontWeight,
                  letterSpacing: computed.letterSpacing,
                  textTransform: computed.textTransform,
                  lineHeight: computed.lineHeight,
                };
              };

              const readLayout = (node) => {
                const computed = window.getComputedStyle(node);
                return {
                  justifyContent: computed.justifyContent,
                  paddingTop: computed.paddingTop,
                  paddingRight: computed.paddingRight,
                  paddingBottom: computed.paddingBottom,
                  paddingLeft: computed.paddingLeft,
                };
              };

              return {
                sectionLabel: read(sectionLabel),
                sectionLabelExpected: resolveStyles({
                  fontFamily: '"Syne", sans-serif',
                  fontSize: '0.62rem',
                  fontWeight: '700',
                  letterSpacing: '0.1em',
                  textTransform: 'uppercase',
                }),
                fieldLabel: read(fieldLabel),
                fieldLabelExpected: resolveStyles({
                  fontFamily: '"Syne", sans-serif',
                  fontSize: '0.58rem',
                  fontWeight: '700',
                  letterSpacing: '0.1em',
                  textTransform: 'uppercase',
                }),
                browseButton: read(browseButton),
                browseButtonExpected: resolveStyles({
                  fontFamily: '"DM Sans", sans-serif',
                  fontSize: '0.78rem',
                  fontWeight: '600',
                }),
                actionFooter: readLayout(actionFooter),
                actionFooterExpected: {
                  justifyContent: 'flex-end',
                  paddingTop: '12px',
                  paddingRight: '0px',
                  paddingBottom: '0px',
                  paddingLeft: '0px',
                },
              };
            }
            """
        )

        for key in ("sectionLabel", "fieldLabel", "browseButton"):
            styles[key]["fontFamily"] = _normalize_font_family(styles[key]["fontFamily"])
            styles[f"{key}Expected"]["fontFamily"] = _normalize_font_family(
                styles[f"{key}Expected"]["fontFamily"]
            )

        assert styles["sectionLabel"] == styles["sectionLabelExpected"]
        assert styles["fieldLabel"] == styles["fieldLabelExpected"]
        assert styles["browseButton"] == styles["browseButtonExpected"]
        assert styles["actionFooter"] == styles["actionFooterExpected"]

    def test_utilities_converter_header_icon_and_title_accent_match_prototype(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        styles = converter.page.evaluate(
            """
            () => {
              const resolveStyles = (declarations) => {
                const probe = document.createElement('div');
                probe.style.position = 'absolute';
                probe.style.visibility = 'hidden';
                Object.assign(probe.style, declarations);
                document.body.appendChild(probe);
                const computed = window.getComputedStyle(probe);
                const result = {
                  backgroundColor: computed.backgroundColor,
                  color: computed.color,
                };
                probe.remove();
                return result;
              };

              const icon = document.querySelector("[data-testid='utilities-converter-card'] .utility-tool-header-icon");
              const accent = document.querySelector("[data-testid='utilities-converter-card'] .utility-tool-header-title span");
              if (!icon || !accent) {
                throw new Error("Utilities converter header nodes were not found");
              }

              const iconComputed = window.getComputedStyle(icon);
              const accentComputed = window.getComputedStyle(accent);

              return {
                icon: {
                  backgroundColor: iconComputed.backgroundColor,
                  color: iconComputed.color,
                },
                iconExpected: resolveStyles({
                  backgroundColor: "var(--pb-interactive-dim)",
                  color: "var(--pb-interactive)",
                }),
                accentColor: accentComputed.color,
                accentExpected: resolveStyles({
                  color: "var(--pb-brand)",
                }).color,
              };
            }
            """
        )

        assert styles["icon"] == styles["iconExpected"]
        assert styles["accentColor"] == styles["accentExpected"]

    def test_utilities_converter_source_dropdown_floats_beyond_card_without_layout_shift(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        setup_card = authed_page.locator(
            "[data-testid='utilities-converter-page'] .utility-workspace-shell"
        ).first
        trigger = authed_page.locator(
            "[data-testid='utilities-converter-source-format'] [data-dropdown-select-trigger]"
        ).first
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first

        before = setup_card.bounding_box()
        assert before is not None

        trigger.click()
        panel.wait_for(state="visible", timeout=5000)

        after = setup_card.bounding_box()
        panel_box = panel.bounding_box()

        assert after is not None
        assert panel_box is not None
        assert abs(after["height"] - before["height"]) < 1
        assert panel_box["y"] + panel_box["height"] > after["y"] + after["height"] + 2

    def test_utilities_converter_source_dropdown_hover_uses_shared_blue_highlight(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        trigger = authed_page.locator(
            "[data-testid='utilities-converter-source-format'] [data-dropdown-select-trigger]"
        ).first
        panel = authed_page.locator("[data-dropdown-select-panel]:visible").first
        options = panel.locator("[data-dropdown-option]")

        trigger.click()
        panel.wait_for(state="visible", timeout=5000)
        options.nth(1).dispatch_event("mouseover")
        _wait_for_animation_frames(authed_page, 12)

        styles = authed_page.evaluate(
            """
            () => {
              const readAlpha = (value) => {
                const parts = value.split("/");
                if (parts.length < 2) {
                  return 1;
                }
                return parseFloat(parts[parts.length - 1].replace(")", "").trim());
              };
              const panel = Array.from(document.querySelectorAll("[data-dropdown-select-panel]"))
                .find((node) => {
                  const style = window.getComputedStyle(node);
                  return style.display !== "none" && style.visibility !== "hidden";
                });
              const option = panel?.querySelector("[data-dropdown-option]:nth-child(2)");
              if (!option) {
                throw new Error("Converter dropdown option not found");
              }
              const probe = document.createElement('div');
              probe.style.position = 'absolute';
              probe.style.visibility = 'hidden';
              probe.style.backgroundColor = 'var(--pb-info-dim)';
              document.body.appendChild(probe);
              const expected = window.getComputedStyle(probe).backgroundColor;
              probe.remove();
              return {
                className: option.className,
                backgroundColor: window.getComputedStyle(option).backgroundColor,
                expectedBackgroundColor: expected,
                backgroundAlpha: readAlpha(window.getComputedStyle(option).backgroundColor),
                expectedBackgroundAlpha: readAlpha(expected),
              };
            }
            """
        )

        assert "dropdown-select-option-active" in styles["className"]
        assert styles["backgroundColor"] != "color(srgb 0 0 0 / 0)"
        assert abs(styles["backgroundAlpha"] - styles["expectedBackgroundAlpha"]) < 0.02

    def test_utilities_converter_navigation_from_overview_has_no_page_errors(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        errors: list[str] = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))

        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()
        utilities.converter_card.click()
        authed_page.wait_for_url("**/utilities/converter", timeout=5000)

        converter = UtilitiesConverterPage(authed_page, seeded_server)
        assert converter.page_root.is_visible()
        assert not errors

    def test_utilities_converter_dropdowns_use_shared_local_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        converter.select_source_format("pdf")
        assert converter.dropdown_label("utilities-converter-source-format") == "PDF (.pdf)"
        assert converter.pdf_quality.is_visible()

        converter.select_pdf_quality("low")
        assert (
            converter.dropdown_label("utilities-converter-pdf-quality") == "Low (150 DPI, JPEG 80%)"
        )

    def test_utilities_converter_preview_shows_output_extension(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.route(
            "**/api/v1/utilities/convert/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="""
                {
                  "source_format": "cbr",
                  "target_format": "cbz",
                  "total_count": 1,
                  "total_size_bytes": 4096,
                  "lossless": true,
                  "files": [
                    {
                      "path": "/tmp/batman_001.cbr",
                      "output_path": "/tmp/Batman_001.cbz",
                      "size_bytes": 4096
                    }
                  ]
                }
                """,
            ),
        )

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.scope = "manual";
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 }
              ];
              data.preview = null;
              data.validationError = "";
            }
            """
        )

        authed_page.evaluate(
            """
            async () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              await data.runPreview();
            }
            """
        )

        preview_panel = authed_page.locator("[data-testid='utilities-converter-preview-panel']")
        preview_panel.wait_for(state="visible", timeout=5000)
        assert preview_panel.get_by_text("/tmp/Batman_001.cbz", exact=True).is_visible()
        assert preview_panel.get_by_text("/tmp/batman_001.cbr", exact=True).is_visible()
        assert preview_panel.get_by_text("Output File", exact=True).is_visible()
        assert preview_panel.get_by_text("Source File", exact=True).is_visible()
        assert authed_page.locator("[data-testid='utilities-converter-preview-table']").is_visible()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.clearPreview();
            }
            """
        )
        preview_panel.wait_for(state="hidden", timeout=5000)

    def test_utilities_converter_selected_files_use_table_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 },
                { path: "/tmp/batman_002.cbr", name: "batman_002.cbr", size: 2048 }
              ];
            }
            """
        )

        selected_table = authed_page.locator(
            "[data-testid='utilities-converter-selected-files-table']"
        )
        selected_table.wait_for(state="visible", timeout=5000)
        assert selected_table.get_by_text("File", exact=True).is_visible()
        assert selected_table.get_by_text("Size", exact=True).is_visible()
        assert selected_table.get_by_text("batman_001.cbr", exact=True).is_visible()

    def test_utilities_converter_selected_file_remove_button_uses_small_icon_action_size(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 }
              ];
            }
            """
        )

        remove_button = authed_page.locator(
            "[data-testid='utilities-converter-selected-files-table'] button[aria-label='Remove file']"
        ).first
        remove_button.wait_for(state="visible", timeout=5000)
        size = remove_button.bounding_box()

        assert size is not None
        assert round(size["width"]) == 32
        assert round(size["height"]) == 32

    def test_utilities_converter_action_footer_bottom_gap_matches_card_top_rhythm(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        metrics = authed_page.evaluate(
            """
            () => {
              const browseButton = document.querySelector("[data-testid='utilities-converter-browse-files']");
              const startButton = document.querySelector("[data-testid='utilities-converter-start']");
              const card = browseButton ? browseButton.closest(".section-card") : null;
              if (!card || !browseButton || !startButton) {
                throw new Error("Utilities converter rhythm nodes were not found");
              }
              const cardRect = card.getBoundingClientRect();
              const browseRect = browseButton.getBoundingClientRect();
              const startRect = startButton.getBoundingClientRect();
              return {
                topGap: browseRect.top - cardRect.top,
                bottomGap: cardRect.bottom - startRect.bottom,
              };
            }
            """
        )

        assert abs(metrics["topGap"] - metrics["bottomGap"]) <= 1

    def test_utilities_converter_start_button_right_aligns_with_selected_files_table(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 }
              ];
            }
            """
        )

        authed_page.locator("[data-testid='utilities-converter-selected-files-table']").wait_for(
            state="visible", timeout=5000
        )

        metrics = authed_page.evaluate(
            """
            () => {
              const table = document.querySelector("[data-testid='utilities-converter-selected-files-table']");
              const startButton = document.querySelector("[data-testid='utilities-converter-start']");
              if (!table || !startButton) {
                throw new Error("Utilities converter alignment nodes were not found");
              }
              const tableRect = table.getBoundingClientRect();
              const startRect = startButton.getBoundingClientRect();
              return {
                tableRight: tableRect.right,
                startRight: startRect.right,
              };
            }
            """
        )

        assert abs(metrics["tableRight"] - metrics["startRight"]) <= 1.5

    def test_utilities_converter_file_browser_backdrop_covers_full_viewport(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.set_viewport_size({"width": 1440, "height": 960})
        authed_page.locator("[data-testid='utilities-converter-browse-files']").click()

        backdrop = authed_page.locator("[data-testid='file-browser-backdrop']").first
        backdrop.wait_for(state="visible", timeout=5000)

        metrics = authed_page.evaluate(
            """
            () => {
              const backdrop = document.querySelector("[data-testid='file-browser-backdrop']");
              if (!backdrop) {
                throw new Error("File browser backdrop not found");
              }
              const rect = backdrop.getBoundingClientRect();
              return {
                top: rect.top,
                left: rect.left,
                width: rect.width,
                height: rect.height,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
              };
            }
            """
        )

        assert abs(metrics["top"]) <= 1
        assert abs(metrics["left"]) <= 1
        assert abs(metrics["width"] - metrics["viewportWidth"]) <= 1
        assert abs(metrics["height"] - metrics["viewportHeight"]) <= 1

    def test_utilities_converter_file_browser_keeps_previous_directory_visible_while_loading(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        responses = [
            {
                "parent": "/",
                "path": "/tmp",
                "directories": [{"name": "Batman", "path": "/tmp/Batman"}],
                "files": [],
                "quick_links": [],
            },
            {
                "parent": "/tmp",
                "path": "/tmp/Batman",
                "directories": [{"name": "Annuals", "path": "/tmp/Batman/Annuals"}],
                "files": [
                    {"name": "batman_001.cbr", "path": "/tmp/Batman/batman_001.cbr", "size": 4096}
                ],
                "quick_links": [],
            },
        ]
        request_count = {"value": 0}

        def fulfill_browser(route) -> None:  # type: ignore[no-untyped-def]
            payload = responses[min(request_count["value"], len(responses) - 1)]
            if request_count["value"] == 1:
                time.sleep(0.5)
            request_count["value"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        authed_page.route("**/api/v1/filesystem/browse?*", fulfill_browser)

        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.locator("[data-testid='utilities-converter-browse-files']").click()
        modal = authed_page.locator("[data-testid='file-browser-modal']").first
        modal.wait_for(state="visible", timeout=5000)
        first_dir = modal.locator("[data-testid='file-browser-directory-entry']").first
        first_dir.wait_for(state="visible", timeout=5000)
        assert first_dir.get_by_text("Batman", exact=True).is_visible()

        first_dir.click()
        _wait_for_animation_frames(authed_page)

        assert modal.locator("[data-testid='file-browser-directory-entry']").count() == 1
        assert modal.locator("[data-testid='file-browser-directory-entry']").first.is_visible()

        authed_page.wait_for_function(
            """() => {
              const entries = document.querySelectorAll("[data-testid='file-browser-directory-entry']");
              return entries.length > 0 && Array.from(entries).some((entry) => entry.textContent.includes("Annuals"));
            }"""
        )
        assert (
            modal.locator("[data-testid='file-browser-directory-entry']")
            .first.get_by_text("Annuals", exact=True)
            .is_visible()
        )

    def test_utilities_converter_removing_file_keeps_preview_visible_while_refreshing(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        def fulfill_preview(route) -> None:  # type: ignore[no-untyped-def]
            payload = json.loads(route.request.post_data or "{}")
            file_paths = payload.get("file_paths", [])
            if len(file_paths) == 1:
                time.sleep(0.6)
            body = {
                "source_format": "cbr",
                "target_format": "cbz",
                "total_count": len(file_paths),
                "total_size_bytes": 4096 * len(file_paths),
                "lossless": True,
                "files": [
                    {
                        "path": file_path,
                        "output_path": file_path.rsplit(".", 1)[0] + ".cbz",
                        "size_bytes": 4096,
                    }
                    for file_path in file_paths
                ],
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        authed_page.route("**/api/v1/utilities/convert/preview", fulfill_preview)

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.scope = "manual";
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 },
                { path: "/tmp/batman_002.cbr", name: "batman_002.cbr", size: 4096 }
              ];
              data.preview = null;
              data.validationError = "";
            }
            """
        )

        authed_page.evaluate(
            """
            async () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              await data.runPreview();
            }
            """
        )

        preview_panel = authed_page.locator("[data-testid='utilities-converter-preview-panel']")
        preview_panel.wait_for(state="visible", timeout=5000)
        assert preview_panel.get_by_text("/tmp/batman_001.cbz", exact=True).is_visible()
        assert preview_panel.get_by_text("/tmp/batman_002.cbz", exact=True).is_visible()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.removeSelectedFileAt(0);
            }
            """
        )

        _wait_for_animation_frames(authed_page)
        assert preview_panel.is_visible()
        assert authed_page.locator("[data-testid='utilities-converter-preview-table']").is_visible()
        assert preview_panel.get_by_text("/tmp/batman_002.cbz", exact=True).is_visible()

        authed_page.wait_for_function(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              return !!data && !!data.preview && data.previewLoading === false && data.preview.total_count === 1;
            }
            """
        )
        assert preview_panel.get_by_text("/tmp/batman_001.cbz", exact=True).count() == 0
        assert preview_panel.get_by_text("/tmp/batman_002.cbz", exact=True).is_visible()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.removeSelectedFileAt(0);
            }
            """
        )

        preview_panel.wait_for(state="hidden", timeout=5000)

    def test_utilities_converter_submission_includes_effective_trash_folder(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        converter = UtilitiesConverterPage(authed_page, seeded_server)
        converter.goto()

        authed_page.route(
            "**/api/v1/utilities/jobs",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"job_id":"job-converter-1"}',
            ),
        )

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-converter-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities converter Alpine state not found");
              }
              data.scope = "manual";
              data.selectedFiles = [
                { path: "/tmp/batman_001.cbr", name: "batman_001.cbr", size: 4096 }
              ];
              data.validationError = "";
              data.submitting = false;
            }
            """
        )

        with authed_page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/api/v1/utilities/jobs" in response.url
            )
        ) as create_response:
            converter.start_button.click()

        payload = create_response.value.request.post_data_json or {}
        settings = get_settings()
        expected_trash = str(
            resolve_utility_directory(
                db_value="",
                default_parent=settings.library_root,
                default_subdir=".trash",
                library_root=settings.library_root,
                data_dir=settings.data_dir,
            )
        )
        assert payload["config"]["trash_folder"] == expected_trash

    def test_mass_convert_card_navigates_to_workflow_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.mass_convert_card.click()
        authed_page.wait_for_url("**/utilities/mass-convert", timeout=5000)

        page = UtilitiesMassConvertPage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.preview_table.is_visible()
        assert page.footer_dock.is_visible()
        assert page.scope_button("library").is_visible()
        assert page.scope_button("folder").is_visible()
        assert page.scope_button("files").is_visible()
        assert page.scope_button("folder").get_attribute("aria-pressed") == "true"
        assert page.scope_button("files").get_attribute("aria-pressed") == "false"
        assert page.trash_folder_input.input_value() != ""
        assert page.start_button.is_disabled()
        assert authed_page.get_by_text("Rename to template").count() == 0
        assert page.browse_folder_button.is_visible()
        assert page.scope_button("folder").inner_text().strip() == "Select folders"
        assert page.browse_folder_button.inner_text().strip() == "Browse folders"
        assert "2 of 3" in page.footer_dock.inner_text()
        assert "Mass Convert" in page.footer_dock.inner_text()

    def test_mass_convert_library_scope_hides_browse_button_and_autoloads_preview(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/utilities/mass-convert/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "scope": "library",
                        "item_count": 2,
                        "items": [
                            {
                                "file_path": "/library/Batman (2016)/Batman 001.cbr",
                                "source_name": "Batman 001.cbr",
                                "source_format": "CBR",
                                "output_name": "Batman 001.cbz",
                                "size_bytes": 1048576,
                            },
                            {
                                "file_path": "/library/Batman (2016)/Batman 002.pdf",
                                "source_name": "Batman 002.pdf",
                                "source_format": "PDF",
                                "output_name": "Batman 002.cbz",
                                "size_bytes": 2097152,
                            },
                        ],
                    }
                ),
            ),
        )

        page = UtilitiesMassConvertPage(authed_page, seeded_server)
        page.goto()
        page.choose_scope("library")

        page.preview_table.get_by_text("Batman 001.cbr", exact=True).wait_for(
            state="visible",
            timeout=5000,
        )
        page.browse_folder_button.wait_for(state="hidden", timeout=5000)

        assert page.scope_button("library").get_attribute("aria-pressed") == "true"
        assert page.browse_folder_button.is_visible() is False
        assert page.preview_table.get_by_text("Batman 001.cbr", exact=True).is_visible()
        assert page.preview_table.get_by_text("Batman 001.cbz", exact=True).is_visible()
        assert (
            page.workspace.get_by_text(
                "Queue the whole tracked library through the CBZ pipeline.", exact=False
            ).count()
            == 0
        )

    def test_mass_convert_preview_table_uses_series_hover_row_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/utilities/mass-convert/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "scope": "library",
                        "item_count": 1,
                        "items": [
                            {
                                "file_path": "/library/Batman (2016)/Batman 001.cbr",
                                "source_name": "Batman 001.cbr",
                                "source_format": "CBR",
                                "output_name": "Batman 001.cbz",
                                "size_bytes": 1048576,
                            }
                        ],
                    }
                ),
            ),
        )

        page = UtilitiesMassConvertPage(authed_page, seeded_server)
        page.goto()
        page.choose_scope("library")

        row = page.preview_table.locator("tbody tr").first
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
        _wait_for_animation_frames(authed_page)
        after_hover = cell.evaluate("node => window.getComputedStyle(node).backgroundColor")

        assert after_hover == expected_hover_bg
        assert after_hover != before_hover

    def test_mass_convert_submit_uses_scope_and_effective_trash_defaults(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesMassConvertPage(authed_page, seeded_server)
        page.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector('[data-testid="utilities-mass-convert-page"]');
              const data = window.Alpine.$data(root);
              data.scope = 'folder';
              data.selectedFolder = '/tmp/comics/Batman (2016)';
              data.trashFolder = '';
              data.validationError = '';
              data.submitting = false;
            }
            """
        )

        with authed_page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/api/v1/utilities/jobs" in response.url
            )
        ) as create_response:
            page.start_button.click()

        payload = create_response.value.request.post_data_json or {}
        settings = get_settings()
        expected_trash = str(
            resolve_utility_directory(
                db_value="",
                default_parent=settings.library_root,
                default_subdir=".trash",
                library_root=settings.library_root,
                data_dir=settings.data_dir,
            )
        )

        assert payload["job_type"] == "mass_convert_pipeline"
        assert payload["display_name"] == "Mass Convert to CBZ"
        assert payload["config"]["scope"] == "folder"
        assert payload["config"]["scan_folder"] == "/tmp/comics/Batman (2016)"
        assert payload["config"]["trash_folder"] == expected_trash
        assert "file_paths" not in payload["config"]

    def test_mass_convert_submit_supports_multiple_selected_folders(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesMassConvertPage(authed_page, seeded_server)
        page.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector('[data-testid="utilities-mass-convert-page"]');
              const data = window.Alpine.$data(root);
              data.scope = 'folder';
              data.selectedFolders = [
                { path: '/tmp/comics/Batman (2016)', name: 'Batman (2016)' },
                { path: '/tmp/comics/Saga (2012)', name: 'Saga (2012)' },
              ];
              data.selectedFolder = '/tmp/comics/Batman (2016)';
              data.previewLoaded = true;
              data.preview = { scope: 'folder', item_count: 4, total_size_bytes: 0, items: [] };
              data.trashFolder = '';
              data.validationError = '';
              data.submitting = false;
            }
            """
        )

        with authed_page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/api/v1/utilities/jobs" in response.url
            )
        ) as create_response:
            page.start_button.click()

        payload = create_response.value.request.post_data_json or {}
        assert payload["config"]["scope"] == "folder"
        assert payload["config"]["scan_folders"] == [
            "/tmp/comics/Batman (2016)",
            "/tmp/comics/Saga (2012)",
        ]

    def test_integrity_card_navigates_to_workflow_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.integrity_card.click()
        authed_page.wait_for_url("**/utilities/integrity", timeout=5000)

        page = UtilitiesIntegrityPage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.browse_button.is_hidden()
        assert page.start_button.is_visible()
        assert page.header.get_by_text("INTEGRITY CHECK", exact=False).is_visible()
        assert page.header.get_by_text(
            "Archive validation — quick scan or deep decode", exact=True
        ).is_visible()
        assert page.quick_mode_card.get_by_text(
            "Open archive and count pages", exact=True
        ).is_visible()
        assert page.scope_button("library").get_attribute("aria-pressed") == "true"
        assert page.scope_button("folder").get_attribute("aria-pressed") == "false"
        assert page.scope_button("files").get_attribute("aria-pressed") == "false"
        assert page.library_scope_button.inner_text().strip() == "All tracked files"
        assert page.scope_button("folder").inner_text().strip() == "Select folders"
        assert page.remediation_report_button.is_visible()
        assert page.remediation_quarantine_button.is_visible()
        assert page.requeue_search_checkbox.is_visible()
        assert page.remediation_report_button.get_attribute("aria-pressed") == "true"
        assert page.remediation_quarantine_button.get_attribute("aria-pressed") == "false"
        assert page.requeue_search_checkbox.is_checked() is False
        assert page.browse_button.is_hidden()
        assert "Integrity Check" in page.footer_dock.inner_text()
        assert "Quick" in page.footer_dock.inner_text()
        assert "All tracked files" in page.footer_dock.inner_text()
        assert "Entire library" not in page.page_root.inner_text()
        assert "Archive headers only" not in page.page_root.inner_text()

        page.choose_depth("deep")
        assert authed_page.locator("[data-testid='utilities-integrity-depth-deep']").get_attribute(
            "class"
        )
        page.choose_scope("folder")
        assert page.browse_button.is_visible()
        page.choose_scope("files")
        assert page.scope_button("files").get_attribute("class")

    def test_integrity_submit_supports_multiple_selected_folders(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesIntegrityPage(authed_page, seeded_server)
        page.goto()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector('[data-testid="utilities-integrity-page"]');
              const data = window.Alpine.$data(root);
              data.scope = 'folder';
              data.corruptFileAction = 'quarantine';
              data.requeueReplacements = true;
              data.selectedFolders = [
                { path: '/tmp/comics/Batman (2016)', name: 'Batman (2016)' },
                { path: '/tmp/comics/Saga (2012)', name: 'Saga (2012)' },
              ];
              data.selectedFolder = '/tmp/comics/Batman (2016)';
              data.validationError = '';
              data.submitting = false;
            }
            """
        )

        with authed_page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/api/v1/utilities/jobs" in response.url
            )
        ) as create_response:
            page.start_button.click()

        payload = create_response.value.request.post_data_json or {}
        assert payload["job_type"] == "integrity_check"
        assert payload["config"]["scope"] == "folder"
        assert payload["config"]["scan_folders"] == [
            "/tmp/comics/Batman (2016)",
            "/tmp/comics/Saga (2012)",
        ]
        assert payload["config"]["corrupt_action"] == "quarantine"
        assert payload["config"]["requeue_search"] is True

    def test_mass_rename_card_navigates_to_workflow_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.mass_rename_card.click()
        authed_page.wait_for_url("**/utilities/mass-rename", timeout=5000)

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.preview_table.is_visible()
        assert page.edit_templates_link.is_visible()
        assert page.start_button.is_visible()
        assert page.header.get_by_text("MASS RENAME", exact=False).is_visible()
        assert page.header.get_by_text(
            "Apply naming rules with auto-preview", exact=True
        ).is_visible()
        assert page.scope_button("library").get_attribute("aria-pressed") == "false"
        assert page.scope_button("folder").get_attribute("aria-pressed") == "true"
        assert page.files_target.get_attribute("aria-pressed") == "true"
        assert page.folders_target.get_attribute("aria-pressed") == "false"
        assert page.scope_button("folder").inner_text().strip() == "Select folders"
        assert page.browse_button.is_visible()
        assert "Files" in page.footer_dock.inner_text()
        assert "Select folders" in page.footer_dock.inner_text()
        assert "Apply renames" in page.page_root.inner_text()
        assert page.page_root.get_by_text("Refresh", exact=True).count() == 0

    def test_mass_rename_auto_preview_updates_and_folder_target_hides_single_folder_scope(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        preview_responses = [
            {
                "item_count": 1,
                "actionable_count": 1,
                "items": [
                    {
                        "file_path": "/library/Batman_001.cbz",
                        "current_name": "Batman_001.cbz",
                        "proposed_name": "Batman (2016) #001 (Digital).cbz",
                        "template_label": "Issue Template",
                        "status": "ready",
                        "actionable": True,
                    }
                ],
            },
            {
                "item_count": 1,
                "actionable_count": 1,
                "items": [
                    {
                        "file_path": "/library/Batman (2016)",
                        "current_name": "Batman (2016)",
                        "proposed_name": "Batman (2016)",
                        "template_label": "Folder Template",
                        "status": "ready",
                        "actionable": True,
                    }
                ],
            },
            {
                "item_count": 2,
                "actionable_count": 2,
                "items": [
                    {
                        "file_path": "/library/DC/Batman (2016)",
                        "current_name": "Batman (2016)",
                        "proposed_name": "Batman (2016)",
                        "template_label": "Folder Template",
                        "status": "ready",
                        "actionable": True,
                    },
                    {
                        "file_path": "/library/Image/Saga (2012)",
                        "current_name": "Saga (2012)",
                        "proposed_name": "Saga (2012)",
                        "template_label": "Folder Template",
                        "status": "ready",
                        "actionable": True,
                    },
                ],
            },
        ]
        request_payloads: list[dict[str, object]] = []

        def fulfill_preview(route) -> None:  # type: ignore[no-untyped-def]
            request_payloads.append(route.request.post_data_json or {})
            payload = preview_responses[min(len(request_payloads) - 1, len(preview_responses) - 1)]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        authed_page.route("**/api/v1/utilities/rename/preview", fulfill_preview)

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        page.goto()

        assert request_payloads == []
        assert page.scope_button("folder").get_attribute("aria-pressed") == "true"
        assert page.browse_button.is_visible()

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-mass-rename-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities mass rename Alpine state not found");
              }
              data.applyRenameFolderSelection({
                mode: "directories",
                directories: [
                  { path: "/library/DC/Batman (2016)", name: "Batman (2016)" },
                  { path: "/library/Image/Saga (2012)", name: "Saga (2012)" },
                ],
              });
            }
            """
        )

        authed_page.wait_for_function(
            """
            () => {
              const rows = document.querySelectorAll("[data-testid='utilities-mass-rename-preview-table'] tbody tr");
              return rows.length === 1;
            }
            """
        )
        assert request_payloads[0]["scope"] == "folder"
        assert request_payloads[0]["target"] == "files"
        assert request_payloads[0]["file_paths"] == [
            "/library/DC/Batman (2016)",
            "/library/Image/Saga (2012)",
        ]
        assert "Select folders" in page.footer_dock.inner_text()
        assert page.footer_dock.inner_text().find("1") != -1

        page.choose_scope("library")

        authed_page.wait_for_function(
            """
            () => {
              const browse = document.querySelector("[data-testid='utilities-mass-rename-browse']");
              return browse && window.getComputedStyle(browse).display === "none";
            }
            """
        )

        assert request_payloads[-1]["scope"] == "library"

        page.choose_target("folders")

        authed_page.wait_for_function(
            """
            () => {
              const targetFolders = document.querySelector("[data-testid='utilities-mass-rename-target-folders']");
              const singleFolder = document.querySelector("[data-testid='utilities-mass-rename-scope-folder']");
              return (
                targetFolders?.getAttribute("aria-pressed") === "true" &&
                singleFolder &&
                window.getComputedStyle(singleFolder).display === "none"
              );
            }
            """
        )
        authed_page.wait_for_function(
            """
            () => (
              (document.querySelector("[data-testid='utilities-mass-rename-scope-manual']")?.textContent || "")
                .trim() === "Select folders"
            )
            """,
            timeout=5000,
        )

        assert page.scope_button("manual").inner_text().strip() == "Select folders"

        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-mass-rename-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities mass rename Alpine state not found");
              }
              data.applyRenameSelection({
                mode: "directory",
                path: "/library/DC/Batman (2016)",
                name: "Batman (2016)",
              });
              data.applyRenameSelection({
                mode: "directory",
                path: "/library/Image/Saga (2012)",
                name: "Saga (2012)",
              });
            }
            """
        )

        authed_page.wait_for_function(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-mass-rename-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              const rows = document.querySelectorAll("[data-testid='utilities-mass-rename-preview-table'] tbody tr");
              return !!data && data.previewLoading === false && rows.length === 2;
            }
            """,
            timeout=5000,
        )

        assert request_payloads[-1]["target"] == "folders"
        assert request_payloads[-1]["scope"] == "manual"
        assert request_payloads[-1]["file_paths"] == [
            "/library/DC/Batman (2016)",
            "/library/Image/Saga (2012)",
        ]

    def test_mass_rename_preview_table_aligns_with_other_tool_tables(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        page.goto()

        alignment = authed_page.evaluate(
            """
            () => {
              const templateTable = document
                .querySelector("[data-testid='utilities-mass-rename-workspace']")
                ?.querySelector(".utility-tool-table-wrap");
              const previewTable = document.querySelector("[data-testid='utilities-mass-rename-preview-table']");
              if (!templateTable || !previewTable) {
                return null;
              }
              const templateRect = templateTable.getBoundingClientRect();
              const previewRect = previewTable.getBoundingClientRect();
              return {
                leftDelta: Math.abs(templateRect.left - previewRect.left),
                rightDelta: Math.abs(templateRect.right - previewRect.right),
              };
            }
            """
        )

        assert alignment is not None
        assert alignment["leftDelta"] <= 2
        assert alignment["rightDelta"] <= 2

    def test_mass_rename_preview_truncated_cells_use_shared_tooltip_contract(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.route(
            "**/api/v1/utilities/rename/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "item_count": 1,
                        "actionable_count": 1,
                        "items": [
                            {
                                "file_path": "/library/very/long/path/Amazing Spider-Man (2022) #012 Special Digital Edition.cbz",
                                "current_name": "Amazing Spider-Man (2022) #012 Special Digital Edition.cbz",
                                "proposed_name": "Amazing Spider-Man (2022) #012 (Digital Remastered Deluxe Edition).cbz",
                                "template_label": "Issue Template",
                                "status": "ready",
                                "actionable": True,
                            }
                        ],
                    }
                ),
            ),
        )

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        page.goto()
        page.choose_scope("manual")
        authed_page.evaluate(
            """
            () => {
              const root = document.querySelector("[data-testid='utilities-mass-rename-page']");
              const data = root && root._x_dataStack ? root._x_dataStack[0] : null;
              if (!data) {
                throw new Error("Utilities mass rename Alpine state not found");
              }
              data.applyRenameSelection({
                mode: "files",
                files: [
                  {
                    path: "/library/very/long/path/Amazing Spider-Man (2022) #012 Special Digital Edition.cbz",
                    name: "Amazing Spider-Man (2022) #012 Special Digital Edition.cbz",
                  },
                ],
              });
            }
            """
        )
        authed_page.wait_for_function(
            """
            () => document.querySelectorAll("[data-testid='utilities-mass-rename-preview-table'] tbody tr").length === 1
            """
        )

        target = authed_page.locator(
            "[data-testid='utilities-mass-rename-preview-table'] tbody tr td:first-child [data-tooltip-measure]"
        ).first
        target.hover()

        authed_page.wait_for_function(
            """() => {
              const el = document.querySelector("[data-testid='utilities-mass-rename-preview-table'] tbody tr td:first-child .tooltip-wrap");
              if (!el) return false;
              const tip = el.getAttribute("data-tip");
              return !!tip && tip.includes("Amazing Spider-Man (2022) #012 Special Digital Edition.cbz");
            }"""
        )

    def test_mass_rename_browser_is_constrained_to_library_roots(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.mass_rename_card.click()
        authed_page.wait_for_url("**/utilities/mass-rename", timeout=5000)

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        with authed_page.expect_response(
            lambda resp: "/api/v1/filesystem/directories" in resp.url,  # type: ignore[arg-type]
        ) as response_info:
            page.browse_button.click()
        payload = response_info.value.json()

        assert "roots=" in response_info.value.request.url
        assert payload["path"] != "/"
        assert payload["quick_links"]
        assert all(link["icon"] == "folder" for link in payload["quick_links"])

    def test_mass_rename_folder_browser_allows_multi_directory_selection(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        preview_payloads: list[dict[str, object]] = []

        def fulfill_directories(route) -> None:  # type: ignore[no-untyped-def]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": "/library",
                        "parent": "/",
                        "directories": [
                            {"name": "Batman (2016)", "path": "/library/DC/Batman (2016)"},
                            {"name": "Saga (2012)", "path": "/library/Image/Saga (2012)"},
                        ],
                        "quick_links": [],
                    }
                ),
            )

        def fulfill_preview(route) -> None:  # type: ignore[no-untyped-def]
            payload = route.request.post_data_json or {}
            preview_payloads.append(payload)
            file_paths = payload.get("file_paths") or []
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "item_count": len(file_paths),
                        "actionable_count": len(file_paths),
                        "items": [
                            {
                                "file_path": path,
                                "current_name": path.rsplit("/", 1)[-1],
                                "proposed_name": path.rsplit("/", 1)[-1],
                                "template_label": "Folder Template",
                                "status": "ready",
                                "actionable": True,
                            }
                            for path in file_paths
                        ],
                    }
                ),
            )

        authed_page.route("**/api/v1/filesystem/directories**", fulfill_directories)
        authed_page.route("**/api/v1/utilities/rename/preview", fulfill_preview)

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        page.goto()
        page.browse_button.click()

        modal = authed_page.locator("[data-testid='file-browser-modal']").first
        modal.wait_for(state="visible", timeout=5000)

        toggles = modal.locator("[data-testid='file-browser-directory-toggle']")
        assert toggles.count() == 2
        toggles.nth(0).click()
        toggles.nth(1).click()

        confirm = modal.locator("[data-testid='file-browser-confirm']").last
        assert confirm.inner_text().strip() == "Add 2 Folders"
        confirm.click()

        authed_page.wait_for_function(
            """
            () => {
              const modal = document.querySelector("[data-testid='file-browser-modal']");
              const rows = document.querySelectorAll("[data-testid='utilities-mass-rename-preview-table'] tbody tr");
              return (!modal || window.getComputedStyle(modal).display === "none") && rows.length === 2;
            }
            """
        )

        assert preview_payloads[-1]["target"] == "files"
        assert preview_payloads[-1]["scope"] == "folder"
        assert preview_payloads[-1]["file_paths"] == [
            "/library/DC/Batman (2016)",
            "/library/Image/Saga (2012)",
        ]

    def test_workflow_back_link_returns_to_utilities_shell_cleanly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.mass_rename_card.click()
        authed_page.wait_for_url("**/utilities/mass-rename", timeout=5000)

        page = UtilitiesMassRenamePage(authed_page, seeded_server)
        page.back_link.click()
        authed_page.wait_for_url("**/utilities", timeout=5000)
        utilities.page_root.wait_for(state="visible", timeout=5000)

        assert utilities.page_root.is_visible()
        assert utilities.overview_panel.is_visible()
        assert authed_page.locator("[data-testid='file-browser-modal']").count() == 0

    def test_browser_back_from_workflow_restores_utilities_shell_cleanly(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.mass_rename_card.click()
        authed_page.wait_for_url("**/utilities/mass-rename", timeout=5000)
        authed_page.go_back(wait_until="domcontentloaded")
        authed_page.wait_for_function(
            "window.location.pathname === '/utilities' && window.location.search.indexOf('tab=utilities') !== -1"
        )
        utilities.page_root.wait_for(state="visible", timeout=5000)
        authed_page.wait_for_function(
            """
            () => {
              const content = document.getElementById('content');
              if (!content) {
                return false;
              }
              return (
                content.getAttribute('data-page-swap-phase') === null &&
                window.getComputedStyle(content).pointerEvents !== 'none'
              );
            }
            """
        )

        assert utilities.page_root.is_visible()
        assert utilities.overview_panel.is_visible()
        assert authed_page.locator("[data-testid='sidebar-mobile-backdrop']").is_hidden()
        assert authed_page.locator("[data-testid='file-browser-modal']").count() == 0

        utilities.switch_tab("queue")
        assert utilities.queue_panel.is_visible()

    def test_browser_back_from_direct_workflow_load_returns_to_utilities_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        # First navigate to utilities so there's a history entry to go back to
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()
        utilities.page_root.wait_for(state="visible", timeout=10000)

        # Then navigate to a workflow page
        authed_page.goto(f"{seeded_server}/utilities/mass-rename")
        authed_page.wait_for_url("**/utilities/mass-rename", timeout=10000)
        authed_page.locator("[data-testid='utilities-mass-rename-page']").first.wait_for(
            state="visible",
            timeout=10000,
        )

        # Browser back should return to the utilities shell
        authed_page.go_back(wait_until="domcontentloaded")
        authed_page.wait_for_url("**/utilities**", timeout=15000)

        utilities.page_root.wait_for(state="visible", timeout=15000)

        assert utilities.page_root.is_visible()
        assert authed_page.locator("[data-testid='sidebar-mobile-backdrop']").is_hidden()

    def test_db_check_card_navigates_to_workflow_page_and_runs_preview(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.db_check_card.click()
        authed_page.wait_for_url("**/utilities/db-check", timeout=5000)

        page = UtilitiesDbCheckPage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.header.get_by_text("DB CHECK", exact=False).is_visible()
        assert page.header.get_by_text("& CLEANUP", exact=False).is_visible()
        assert page.header.get_by_text(
            "Orphaned records, untracked files, path repairs, metadata refresh",
            exact=False,
        ).is_visible()
        assert page.workspace.get_by_text("Checks to Run", exact=True).is_visible()
        assert page.workspace.get_by_text("Library root", exact=False).is_visible()
        assert page.page.locator("[data-testid='utilities-db-check-library-root']").is_disabled()
        assert (
            page.page.locator("[data-testid='utilities-db-check-browse-library-root']").count() == 0
        )
        assert page.workspace.get_by_role("button", name="Run preview").is_visible()
        assert page.workspace.get_by_role("button", name="Start cleanup").is_visible()
        assert page.workspace.get_by_text("Cleanup checks", exact=False).count() == 0
        assert page.workspace.get_by_text("Preview findings", exact=False).count() == 0
        assert page.workspace.get_by_text("Stale file references", exact=False).count() == 0
        assert page.workspace.get_by_text("Missing series paths", exact=False).count() == 0
        assert page.workspace.get_by_text("Rebuild search index", exact=False).count() == 0

        page.run_preview()
        assert page.findings.is_visible()

    def test_db_check_preview_actions_render_side_by_side(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesDbCheckPage(authed_page, seeded_server)
        page.goto()

        page.run_preview()

        action_buttons = (
            page.findings.locator("tbody tr").first.locator("td").last.locator("button")
        )
        assert action_buttons.count() >= 2

        first_box = action_buttons.nth(0).bounding_box()
        second_box = action_buttons.nth(1).bounding_box()

        assert first_box is not None
        assert second_box is not None
        assert abs(first_box["y"] - second_box["y"]) < 4

    def test_db_check_preview_path_uses_shared_tooltip_and_findings_spacing(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1100, "height": 1200})
        page = UtilitiesDbCheckPage(authed_page, seeded_server)
        page.goto()
        page.run_preview()

        finding_measure = (
            page.findings.locator("tbody tr").first.locator("[data-tooltip-measure]").first
        )
        path_measure = (
            page.findings.locator("tbody tr").first.locator("[data-tooltip-measure]").last
        )
        tooltip_wraps = page.findings.locator("tbody tr").first.locator(".tooltip-wrap")
        findings_token_text = page.findings.locator(".utility-token").first.inner_text()
        checks_token_text = page.findings.locator(".utility-token").nth(1).inner_text()

        assert finding_measure.is_visible()
        assert path_measure.is_visible()
        assert tooltip_wraps.nth(0).get_attribute("data-tooltip-auto") == ""
        assert tooltip_wraps.nth(0).get_attribute("data-tip-pos") is None
        assert tooltip_wraps.nth(1).get_attribute("data-tooltip-auto") == ""
        assert tooltip_wraps.nth(1).get_attribute("data-tip-pos") == "left"
        assert " finding" in findings_token_text.lower()
        assert " checks" in checks_token_text.lower()

        finding_cell_box = (
            page.findings.locator("tbody tr").first.locator("td").nth(1).bounding_box()
        )
        path_cell_box = page.findings.locator("tbody tr").first.locator("td").nth(2).bounding_box()

        assert finding_cell_box is not None
        assert path_cell_box is not None
        assert abs(finding_cell_box["width"] - path_cell_box["width"]) < 8
        assert finding_cell_box["width"] > 200

        tooltip_wraps.nth(1).hover()
        tooltip_metrics = tooltip_wraps.nth(1).evaluate(
            """
            (el) => {
              const rect = el.getBoundingClientRect();
              const container = el.closest('.utility-tool-table-wrap');
              const containerRect = container ? container.getBoundingClientRect() : { left: 0 };
              const styles = window.getComputedStyle(el, '::after');
              return {
                maxWidth: parseFloat(styles.maxWidth),
                availableLeft: rect.left - containerRect.left - 16,
              };
            }
            """
        )
        assert tooltip_metrics["maxWidth"] <= tooltip_metrics["availableLeft"] + 1

    def test_export_card_navigates_to_workflow_page_and_reveals_json_options(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        utilities = UtilitiesPage(authed_page, seeded_server)
        utilities.goto()

        utilities.export_card.click()
        authed_page.wait_for_url("**/utilities/export", timeout=5000)

        page = UtilitiesExportPage(authed_page, seeded_server)
        assert page.page_root.is_visible()
        assert page.header.is_visible()
        assert page.workspace.is_visible()
        assert page.start_button.is_visible()
        assert page.header.get_by_text("EXPORT", exact=False).is_visible()
        assert page.header.get_by_text("LIBRARY", exact=False).is_visible()
        assert page.header.get_by_text(
            "CSV or JSON snapshots for audit and migration",
            exact=False,
        ).is_visible()
        assert page.page.get_by_test_id("utilities-export-summary-records").is_visible()
        assert page.page.get_by_test_id("utilities-export-summary-fields").is_visible()
        assert page.json_options.is_visible()

        page.choose_format("csv")
        assert page.json_options.is_hidden()
        page.choose_format("json")
        assert page.json_options.is_visible()
        assert page.pretty_option.is_visible()
        assert page.multi_value_select_all.is_visible()
        assert page.multi_value_clear_all.is_visible()

    def test_export_footer_dock_tracks_format_scope_and_fields(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesExportPage(authed_page, seeded_server)
        page.goto()

        footer = page.footer_dock
        footer.get_by_text("JSON (pretty)", exact=False).wait_for(state="visible", timeout=5000)

        page.choose_format("csv")

        assert footer.get_by_text("CSV", exact=False).is_visible()
        assert footer.get_by_text(str(page.selected_field_count()), exact=False).is_visible()

    def test_export_json_multi_value_controls_select_and_clear_all(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        page = UtilitiesExportPage(authed_page, seeded_server)
        page.goto()

        page.choose_format("json")
        page.multi_value_select_all.click()

        checked_count = page.multi_value_grid.locator("input[type='checkbox']:checked").count()
        assert checked_count > 1

        page.multi_value_clear_all.click()

        checked_after_clear = page.multi_value_grid.locator(
            "input[type='checkbox']:checked"
        ).count()
        assert checked_after_clear == 0
