"""Route-contract tests for the rewritten utilities shell and converter page."""

from __future__ import annotations

import json
import os
import re
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: TC002

from pullbox.config import get_settings
from pullbox.utilities.models import JobState, JobType, UtilityJob
from pullbox.utilities.settings import resolve_utility_directory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-utilities-ui")


async def _seed_utility_job(
    sec_db: async_sessionmaker,
    *,
    job_id: str,
    display_name: str,
    state: JobState = JobState.COMPLETED,
    job_type: JobType = JobType.FILE_CONVERT,
    total_items: int = 4,
    completed_items: int = 4,
    failed_items: int = 0,
    skipped_items: int = 0,
    warning_count: int = 0,
    created_at: str = "2026-05-29T10:00:00Z",
    completed_at: str = "2026-05-29T10:05:00Z",
    error_message: str | None = None,
) -> None:
    async with sec_db() as session:
        session.add(
            UtilityJob(
                id=job_id,
                job_type=job_type,
                display_name=display_name,
                state=state,
                config="{}",
                total_items=total_items,
                completed_items=completed_items,
                failed_items=failed_items,
                skipped_items=skipped_items,
                warning_count=warning_count,
                queue_position=None,
                created_at=created_at,
                started_at=created_at,
                completed_at=completed_at,
                error_message=error_message,
            )
        )
        await session.commit()


@pytest.mark.asyncio
class TestUtilitiesRouteContracts:
    """Verify the utilities area renders stable mounted regions."""

    async def test_utilities_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities")

        assert response.status_code == 200
        assert 'data-testid="utilities-page"' in response.text
        assert 'data-testid="utilities-header"' in response.text
        assert 'data-testid="utilities-gauges"' in response.text
        assert 'data-testid="utilities-shell"' in response.text
        assert 'data-testid="utilities-body"' in response.text
        assert 'data-testid="utilities-tabs"' in response.text
        assert 'data-testid="utilities-tab-history"' in response.text
        assert 'data-testid="utilities-content"' in response.text
        assert 'data-testid="utilities-overview-panel"' in response.text
        assert 'data-testid="utilities-overview-card-converter"' in response.text
        assert 'data-testid="utilities-footer-dock"' in response.text
        assert 'href="/utilities/mass-convert"' in response.text
        assert 'href="/utilities/mass-rename"' in response.text
        assert 'href="/utilities/integrity"' in response.text
        assert 'href="/utilities/db-check"' in response.text
        assert 'href="/utilities/export"' in response.text
        assert 'href="/utilities/permissions"' in response.text
        assert 'data-testid="utilities-overview-card-permissions"' in response.text

    async def test_utilities_bulk_tools_use_bulk_action_badges(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        overview_response = await authenticated_client.get("/utilities")
        mass_convert_response = await authenticated_client.get("/utilities/mass-convert")
        mass_rename_response = await authenticated_client.get("/utilities/mass-rename")

        assert overview_response.status_code == 200
        assert mass_convert_response.status_code == 200
        assert mass_rename_response.status_code == 200
        assert (
            overview_response.text.count('<span class="utility-launch-tag">Bulk Action</span>') == 2
        )
        assert "Batch convert to CBZ with ComicInfo embedding" in mass_convert_response.text
        assert "Apply naming rules with auto-preview" in mass_rename_response.text
        assert mass_convert_response.text.count("Bulk Action") == 1
        assert mass_rename_response.text.count("Bulk Action") == 1

    async def test_utilities_hx_tab_switch_returns_content_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/utilities?tab=queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="utilities-content"' in response.text
        assert 'data-testid="utilities-header"' in response.text
        assert 'data-testid="utilities-tabs"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'data-testid="utilities-queue-panel"' in response.text
        assert 'data-testid="utilities-footer-dock"' in response.text
        assert 'data-testid="utilities-page"' not in response.text

    async def test_utilities_direct_htmx_tab_returns_content_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/htmx/utilities/queue")

        assert response.status_code == 200
        assert 'data-testid="utilities-content"' in response.text
        assert 'data-testid="utilities-header"' in response.text
        assert 'data-testid="utilities-tabs"' in response.text
        assert 'data-testid="utilities-queue-panel"' in response.text
        assert 'data-testid="utilities-footer-dock"' in response.text
        assert 'data-testid="utilities-page"' not in response.text

    async def test_utilities_history_htmx_tab_returns_content_bundle(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_utility_job(
            sec_db,
            job_id="history-tab-contract",
            display_name="History Tab Contract",
        )
        response = await authenticated_client.get("/htmx/utilities/history")

        assert response.status_code == 200
        assert 'data-testid="utilities-content"' in response.text
        assert 'data-testid="utilities-header"' in response.text
        assert 'data-testid="utilities-tabs"' in response.text
        assert 'data-testid="utilities-history-panel"' in response.text
        assert 'data-testid="utilities-history-table"' in response.text
        assert 'data-testid="utilities-footer-dock"' in response.text
        assert 'data-testid="utilities-page"' not in response.text

    async def test_utilities_queue_renders_active_and_queued_jobs_only(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities?tab=queue")

        assert response.status_code == 200
        assert 'data-testid="utilities-queue-panel"' in response.text
        assert 'data-testid="utilities-queue-active-section"' in response.text
        assert 'data-testid="utilities-queue-queued-section"' in response.text
        assert 'data-testid="utilities-history-section"' not in response.text
        assert 'data-testid="utilities-history-table"' not in response.text
        assert response.text.count('data-log-viewer-contract="v1"') == 0
        assert 'data-testid="utilities-queue-active-job"' in response.text
        assert 'data-testid="utilities-queue-active-job-details"' not in response.text
        assert 'data-testid="utilities-queue-active-job-pause"' in response.text
        assert 'data-testid="utilities-queue-active-job-cancel"' in response.text
        assert 'data-tip="Pause job"' in response.text
        assert 'data-tip="Cancel job"' in response.text
        assert 'data-tip="Job details"' not in response.text
        assert 'data-tip="Download"' not in response.text

    async def test_utilities_history_keeps_log_viewer_on_completed_jobs_only(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_utility_job(
            sec_db,
            job_id="history-log-contract",
            display_name="History Log Contract",
        )
        response = await authenticated_client.get("/utilities?tab=history")

        assert response.status_code == 200
        assert 'data-testid="utilities-history-panel"' in response.text
        assert 'data-testid="utilities-history-section"' in response.text
        assert 'data-testid="utilities-queue-active-section"' not in response.text
        assert 'data-testid="utilities-queue-queued-section"' not in response.text
        assert response.text.count('data-log-viewer-contract="v1"') == 0
        assert 'data-testid="utilities-history-job-detail-placeholder"' in response.text
        assert 'hx-get="/htmx/utilities/jobs/history-log-contract/detail"' in response.text
        assert 'data-tip="Job details"' in response.text
        assert 'data-tip="Download"' not in response.text
        assert response.text.count('data-tip="Refresh"') == 0
        initial_data_match = re.search(
            r'<script id="utilities-queue-initial-data" type="application/json">(.*?)</script>',
            response.text,
            flags=re.DOTALL,
        )
        assert initial_data_match is not None
        initial_data = json.loads(initial_data_match.group(1))
        assert initial_data["jobs"] == []
        assert 'x-data="{ job:' not in response.text
        assert "getAllUnresolvableItems(" not in response.text

        detail_response = await authenticated_client.get(
            "/htmx/utilities/jobs/history-log-contract/detail"
        )

        assert detail_response.status_code == 200
        assert 'x-data="{ job:' in detail_response.text
        assert detail_response.text.count('data-log-viewer-contract="v1"') == 1
        assert 'data-search-field-contract="baseline-v2"' in detail_response.text
        assert 'data-dropdown-select-contract="v1"' in detail_response.text
        assert 'data-tip="Download"' in detail_response.text
        assert 'hx-boost="false"' in detail_response.text
        assert 'data-tip="Close"' in detail_response.text
        assert "@click=\"setLevel('DEBUG')\"" in detail_response.text
        assert 'class="btn-ghost btn-sm !min-h-8 !w-8 !px-0 !py-0"' in detail_response.text
        assert (
            'class="w-full min-w-0 max-w-full rounded-xl border border-pb-border overflow-hidden'
            in detail_response.text
        )
        assert (
            'class="log-terminal w-full min-w-0 max-w-full overflow-y-auto overflow-x-hidden'
            in detail_response.text
        )
        assert (
            'class="log-line flex w-full min-w-0 max-w-full gap-0 overflow-hidden'
            in detail_response.text
        )
        assert (
            "x-bind:title=\"[entry.formatted_timestamp || entry.timestamp || ''"
            in detail_response.text
        )
        assert 'class="log-detail min-w-0 max-w-full overflow-hidden' in detail_response.text

    async def test_utilities_history_uses_shared_table_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_utility_job(
            sec_db,
            job_id="history-table-contract",
            display_name="History Table Contract",
        )
        response = await authenticated_client.get("/utilities?tab=history")

        assert response.status_code == 200
        assert "Job History" in response.text
        assert 'data-testid="utilities-history-table"' in response.text
        assert 'data-testid="utilities-history-filter-status"' in response.text
        assert 'data-testid="utilities-history-clear"' in response.text
        assert 'data-testid="utilities-history-sort-job"' in response.text
        assert 'data-testid="utilities-history-sort-status"' in response.text
        assert 'data-testid="utilities-history-sort-items"' in response.text
        assert 'data-testid="utilities-history-sort-completed_at"' in response.text
        assert 'data-testid="utilities-history-rollback"' in response.text
        assert (
            'class="table-base table-fixed min-w-[640px] sm:min-w-[760px] lg:min-w-[920px]"'
            in response.text
        )
        assert "w-44 xl:w-48 table-head-cell-right hidden lg:table-cell" in response.text
        assert (
            'class="table-head-cell table-head-cell-tight table-head-cell-right w-28 xl:w-32"'
            in response.text
        )
        assert response.text.count('data-dropdown-select-contract="v1"') >= 1
        assert response.text.count('data-dropdown-select-mode="htmx"') >= 1
        assert "tab=history" in response.text

    async def test_utilities_history_is_backend_paginated_and_sorted(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        for index in range(30):
            await _seed_utility_job(
                sec_db,
                job_id=f"history-page-{index:02d}",
                display_name=f"Utility History {index:02d}",
                created_at=f"2026-05-29T09:{index:02d}:00Z",
                completed_at=f"2026-05-29T09:{index:02d}:30Z",
            )

        response = await authenticated_client.get(
            "/utilities?tab=history&sort=job&page=2",
            headers={"HX-Request": "true", "HX-Target": "utilities-history-section"},
        )

        assert response.status_code == 200
        assert "Utility History 25" in response.text
        assert "Utility History 29" in response.text
        assert "Utility History 00" not in response.text
        assert 'data-testid="page-dock-pagination"' in response.text
        assert 'hx-target="#utilities-history-section"' in response.text

    async def test_utilities_history_status_filter_is_backend_scoped(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_utility_job(
            sec_db,
            job_id="history-completed",
            display_name="Clean Completed Utility",
        )
        await _seed_utility_job(
            sec_db,
            job_id="history-partial",
            display_name="Partial Utility",
            completed_items=3,
            failed_items=1,
        )

        response = await authenticated_client.get(
            "/utilities?tab=history&utility_history_status=partial",
            headers={"HX-Request": "true", "HX-Target": "utilities-history-section"},
        )

        assert response.status_code == 200
        assert "Partial Utility" in response.text
        assert "Clean Completed Utility" not in response.text
        assert "Partial" in response.text

        completed_response = await authenticated_client.get(
            "/utilities?tab=history&utility_history_status=completed",
            headers={"HX-Request": "true", "HX-Target": "utilities-history-section"},
        )

        assert completed_response.status_code == 200
        assert "Clean Completed Utility" in completed_response.text
        assert "Partial Utility" not in completed_response.text

    async def test_utilities_converter_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/converter")

        assert response.status_code == 200
        assert 'data-testid="utilities-converter-page"' in response.text
        assert 'data-testid="utilities-converter-header"' in response.text
        assert 'data-testid="utilities-converter-workspace"' in response.text
        assert 'data-testid="utilities-converter-card"' in response.text
        assert 'data-testid="utilities-converter-back-link"' in response.text
        assert 'data-testid="utilities-converter-footer-dock"' in response.text
        assert 'data-testid="utilities-converter-source-format"' in response.text
        assert 'data-testid="utilities-converter-pdf-quality"' in response.text
        assert 'data-testid="utilities-converter-selected-files-table"' in response.text
        assert 'data-testid="utilities-converter-preview-table"' in response.text
        assert "Single-file CBR, CB7, PDF → CBZ" in response.text
        assert "Conversion Setup" in response.text
        assert "Files to Convert" in response.text
        assert "Browse" in response.text
        assert "Preview output" not in response.text
        assert "Cancel" in response.text
        assert "Start conversion" in response.text
        assert "Single-file repair" not in response.text
        assert re.search(r">\s*Selection\s*<", response.text) is None
        assert "Target format" not in response.text
        assert (
            'class="btn-ghost btn-sm !min-h-8 !w-8 !px-0 !py-0 '
            'hover:border-pb-error/40 hover:bg-pb-error-dim hover:text-pb-error"' in response.text
        )
        assert 'data-testid="file-browser-backdrop"' in response.text
        assert response.text.count('data-dropdown-select-contract="v1"') >= 2
        assert response.text.count('data-dropdown-select-mode="local"') >= 2

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
        assert f"trashFolder: {json.dumps(expected_trash)}" in response.text
        assert f"trashFolderBrowsePath: {json.dumps(expected_trash)}" in response.text

    async def test_utilities_mass_convert_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/mass-convert")

        assert response.status_code == 200
        assert 'data-testid="utilities-mass-convert-page"' in response.text
        assert 'data-testid="utilities-mass-convert-header"' in response.text
        assert 'data-testid="utilities-mass-convert-workspace"' in response.text
        assert 'data-testid="utilities-mass-convert-card"' in response.text
        assert 'data-testid="utilities-mass-convert-back-link"' in response.text
        assert 'data-testid="utilities-mass-convert-footer-dock"' in response.text
        assert 'data-testid="utilities-mass-convert-browse-files"' in response.text
        assert 'data-testid="utilities-mass-convert-browse-folder"' in response.text
        assert 'data-testid="utilities-mass-convert-scope-library"' in response.text
        assert 'data-testid="utilities-mass-convert-scope-folder"' in response.text
        assert 'data-testid="utilities-mass-convert-scope-files"' in response.text
        assert 'data-testid="utilities-mass-convert-preview-table"' in response.text
        assert 'data-testid="utilities-mass-convert-start"' in response.text
        assert "Batch convert to CBZ with ComicInfo embedding" in response.text
        assert "Pipeline Steps" in response.text
        assert "Convert to CBZ" in response.text
        assert "Embed ComicInfo.xml" in response.text
        assert "Verify integrity" in response.text
        assert "Required" in response.text
        assert "Optional" in response.text
        assert "Conversion Preview" in response.text
        assert "Select folders" in response.text
        assert "Browse folders" in response.text
        assert "Start conversion" in response.text
        assert ">scope<" in response.text.lower()
        assert ">files<" in response.text.lower()
        assert ">steps<" in response.text.lower()
        assert ">tool<" in response.text.lower()
        assert "Step 4" not in response.text
        assert "3-step pipeline" not in response.text
        assert "Pipeline setup" not in response.text
        assert "Scope and source selection" not in response.text
        assert "Clear all" not in response.text
        assert "Step 4" not in response.text
        assert "Rename to template" not in response.text
        assert "Queue the whole tracked library through the CBZ pipeline." not in response.text
        assert (
            "Switch to files when you want a per-file preview before queueing the job."
            not in response.text
        )

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
        assert f"trashFolder: {json.dumps(expected_trash)}" in response.text
        assert f"trashFolderBrowsePath: {json.dumps(expected_trash)}" in response.text
        assert 'class="btn-ghost btn-sm !min-h-10 !w-10 !px-0 !py-0 sm:shrink-0"' in response.text

    async def test_utilities_integrity_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/integrity")

        assert response.status_code == 200
        assert 'data-testid="utilities-integrity-page"' in response.text
        assert 'data-testid="utilities-integrity-header"' in response.text
        assert 'data-testid="utilities-integrity-workspace"' in response.text
        assert 'data-testid="utilities-integrity-card"' in response.text
        assert 'data-testid="utilities-integrity-back-link"' in response.text
        assert 'data-testid="utilities-integrity-footer-dock"' in response.text
        assert 'data-testid="utilities-integrity-browse"' in response.text
        assert 'data-testid="utilities-integrity-depth-quick"' in response.text
        assert 'data-testid="utilities-integrity-depth-deep"' in response.text
        assert 'data-testid="utilities-integrity-scope-library"' in response.text
        assert 'data-testid="utilities-integrity-scope-folder"' in response.text
        assert 'data-testid="utilities-integrity-scope-files"' in response.text
        assert 'data-testid="utilities-integrity-remediation-report"' in response.text
        assert 'data-testid="utilities-integrity-remediation-quarantine"' in response.text
        assert 'data-testid="utilities-integrity-requeue-search"' in response.text
        assert 'data-testid="utilities-integrity-start"' in response.text
        assert "Select folders" in response.text
        assert "selectedFolders.length" in response.text
        assert "Archive validation — quick scan or deep decode" in response.text
        assert "Scan Mode" in response.text
        assert "Scope" in response.text
        assert "When corruption is found" in response.text
        assert "Report only" in response.text
        assert "Trash corrupt files" in response.text
        assert "Search for replacements when linked issues return to Wanted" in response.text
        assert "Start scan" in response.text
        assert "All tracked files" in response.text
        assert "Open archive and count pages" in response.text
        assert ">tool<" in response.text.lower()
        assert ">mode<" in response.text.lower()
        assert ">scope<" in response.text.lower()
        assert "Integrity scan setup" not in response.text
        assert "Scope and source selection" not in response.text
        assert "Choose how deeply Pullbox should inspect" not in response.text
        assert "Entire library" not in response.text
        assert "Archive headers only" not in response.text
        assert (
            "Archive validation with quick scans or full image decode passes." not in response.text
        )

    async def test_utilities_mass_rename_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/mass-rename")

        assert response.status_code == 200
        assert 'data-testid="utilities-mass-rename-page"' in response.text
        assert 'data-testid="utilities-mass-rename-header"' in response.text
        assert 'data-testid="utilities-mass-rename-workspace"' in response.text
        assert 'data-testid="utilities-mass-rename-card"' in response.text
        assert 'data-testid="utilities-mass-rename-back-link"' in response.text
        assert 'data-testid="utilities-mass-rename-footer-dock"' in response.text
        assert 'data-testid="utilities-mass-rename-target-files"' in response.text
        assert 'data-testid="utilities-mass-rename-target-folders"' in response.text
        assert 'data-testid="utilities-mass-rename-scope-library"' in response.text
        assert 'data-testid="utilities-mass-rename-scope-folder"' in response.text
        assert 'data-testid="utilities-mass-rename-scope-manual"' in response.text
        assert 'data-testid="utilities-mass-rename-edit-templates"' in response.text
        assert 'data-testid="utilities-mass-rename-preview-table"' in response.text
        assert 'data-testid="utilities-mass-rename-start"' in response.text
        assert 'data-testid="utilities-mass-rename-preview"' not in response.text
        assert "/settings?tab=media" in response.text
        assert "allowedBrowseRoots" in response.text
        assert "Apply naming rules with auto-preview" in response.text
        assert "Rename Target" in response.text
        assert "Active Naming Templates" in response.text
        assert "Scope" in response.text
        assert "Rename Preview" in response.text
        assert "Apply renames" in response.text
        assert "Collection" in response.text
        assert "Single-release" in response.text
        assert ">Select folders<" in response.text
        assert "selectedFolders.length" in response.text
        assert ">target<" in response.text.lower()
        assert ">scope<" in response.text.lower()
        assert ">changes<" in response.text.lower()
        assert ">tool<" in response.text.lower()
        assert "Collection Template" not in response.text
        assert "Single-Release Template" not in response.text
        assert "Rename rules" not in response.text
        assert "Scope and preview" not in response.text
        assert "Target type" not in response.text
        assert "Run preview" not in response.text
        assert "Queue rename job" not in response.text
        assert "Refresh" not in response.text

    async def test_utilities_db_check_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/db-check")

        assert response.status_code == 200
        assert 'data-testid="utilities-db-check-page"' in response.text
        assert 'data-testid="utilities-db-check-header"' in response.text
        assert 'data-testid="utilities-db-check-workspace"' in response.text
        assert 'data-testid="utilities-db-check-card"' in response.text
        assert 'data-testid="utilities-db-check-back-link"' in response.text
        assert 'data-testid="utilities-db-check-footer-dock"' in response.text
        assert 'data-testid="utilities-db-check-check-orphans"' in response.text
        assert 'data-testid="utilities-db-check-check-stale"' in response.text
        assert 'data-testid="utilities-db-check-check-optimize"' in response.text
        assert "defaultOptimize: false" in response.text
        assert 'data-testid="utilities-db-check-preview"' in response.text
        assert 'data-testid="utilities-db-check-start"' in response.text
        assert 'data-testid="utilities-db-check-browse-library-root"' not in response.text
        assert "disabled" in response.text

    async def test_database_optimize_link_selects_only_database_optimization(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/db-check?check=optimize")

        assert response.status_code == 200
        assert "defaultOptimize: true" in response.text
        assert 'class="flex flex-nowrap items-center justify-end gap-2"' in response.text
        assert "data-tooltip-auto" in response.text
        assert 'data-tip-pos="left"' in response.text
        assert "data-tooltip-measure" in response.text
        assert response.text.count('data-tip-pos="left"') == 1
        assert 'style="table-layout: fixed"' in response.text
        assert 'style="width: calc((100% - 340px) / 2)"' in response.text
        assert 'style="width: 100%; max-width: 100%"' in response.text
        assert (
            "findings.length + (findings.length === 1 ? ' finding' : ' findings')" in response.text
        )
        assert (
            "Orphaned records, untracked files, path repairs, metadata refresh, "
            "storage optimization" in response.text
        )
        assert "Stale file references" not in response.text
        assert "Missing series paths" not in response.text
        assert "Rebuild search index" not in response.text
        assert "Checks to Run" in response.text
        assert "Library root" in response.text
        assert "Start cleanup" in response.text
        assert ">tool<" in response.text.lower()
        assert ">checks<" in response.text.lower()
        assert ">root<" in response.text.lower()
        assert "Cleanup checks" not in response.text
        assert "Database recovery" not in response.text
        assert "Choose the database and filesystem checks" not in response.text
        assert "Preview findings" not in response.text
        assert "Apply fixes" not in response.text

    async def test_utilities_permissions_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/permissions")

        assert response.status_code == 200
        assert 'data-testid="utilities-permissions-page"' in response.text
        assert 'data-testid="utilities-permissions-header"' in response.text
        assert 'class="utility-tool-header-block"' in response.text
        assert 'data-testid="utilities-permissions-card"' in response.text
        assert response.text.count('data-testid="utilities-permissions-card"') == 1
        assert 'data-testid="utilities-permissions-workspace"' in response.text
        assert 'data-testid="utilities-permissions-back-link"' in response.text
        assert 'data-testid="utilities-permissions-footer-dock"' in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert response.text.count("utility-workspace-shell") >= 2
        assert 'data-testid="utilities-permissions-scope-card"' in response.text
        assert 'data-testid="utilities-permissions-options-card"' in response.text
        options_card_index = response.text.index('data-testid="utilities-permissions-options-card"')
        scope_card_index = response.text.index('data-testid="utilities-permissions-scope-card"')
        assert options_card_index < scope_card_index
        assert 'data-testid="utilities-permissions-scope-library"' in response.text
        assert 'data-testid="utilities-permissions-scope-folder"' in response.text
        assert 'data-testid="utilities-permissions-scope-files"' in response.text
        assert 'data-testid="utilities-permissions-browse-folder"' in response.text
        assert 'data-testid="utilities-permissions-browse-files"' in response.text
        assert 'data-testid="utilities-permissions-preview-table"' in response.text
        assert 'data-testid="utilities-permissions-folder-count"' in response.text
        assert 'data-testid="utilities-permissions-file-count"' in response.text
        assert 'data-testid="utilities-permissions-folder-mode"' in response.text
        assert 'data-testid="utilities-permissions-file-mode"' in response.text
        assert 'data-testid="utilities-permissions-confirm-apply"' in response.text
        assert 'data-testid="utilities-permissions-start"' in response.text
        assert "utility-tool-action-footer utility-tool-action-footer-card" in response.text
        assert "Dry-run first" in response.text
        assert "Recursive Library" in response.text
        assert "Permissions" in response.text
        assert "chown" not in response.text.lower()
        assert "chgrp" not in response.text.lower()

    async def test_utilities_permissions_uses_shared_dropdown_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/permissions")

        assert response.status_code == 200
        assert 'data-testid="utilities-permissions-run-mode-select"' in response.text
        assert 'data-testid="utilities-permissions-root-select"' not in response.text
        assert response.text.count('data-dropdown-select-contract="v1"') >= 1
        assert 'data-testid="utilities-permissions-scope-select"' not in response.text
        assert 'class="utility-scope-chip"' in response.text
        assert "<select" not in response.text

    async def test_utilities_export_renders_stable_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/utilities/export")

        assert response.status_code == 200
        assert 'data-testid="utilities-export-page"' in response.text
        assert 'data-testid="utilities-export-header"' in response.text
        assert 'data-testid="utilities-export-workspace"' in response.text
        assert 'data-testid="utilities-export-card"' in response.text
        assert 'data-testid="utilities-export-back-link"' in response.text
        assert 'data-testid="utilities-export-footer-dock"' in response.text
        assert 'data-testid="utilities-export-format-csv"' in response.text
        assert 'data-testid="utilities-export-format-json"' in response.text
        assert 'data-testid="utilities-export-select-all"' in response.text
        assert 'data-testid="utilities-export-summary-records"' in response.text
        assert 'data-testid="utilities-export-summary-fields"' in response.text
        assert 'data-testid="utilities-export-folder"' in response.text
        assert 'data-testid="utilities-export-json-pretty-option"' in response.text
        assert 'data-testid="utilities-export-multi-value-select-all"' in response.text
        assert 'data-testid="utilities-export-multi-value-clear-all"' in response.text
        assert 'data-testid="utilities-export-browse-folder"' in response.text
        assert 'data-testid="utilities-export-start"' in response.text
        assert "Start export" in response.text
        assert "EXPORT LIBRARY" not in response.text
        assert "CSV or JSON snapshots for audit and migration" in response.text
        assert "Scoped output" in response.text
        assert "Output Format" in response.text
        assert "Fields (" in response.text
        assert "Estimated Records" in response.text
        assert "Fields Selected" in response.text
        assert "Series fields" in response.text
        assert "Issue fields" in response.text
        assert "File fields" in response.text
        assert "Publisher fields" in response.text
        assert "Select all" in response.text
        assert "Deselect all" in response.text
        assert "Multi-value fields" in response.text
        assert ">tool<" in response.text.lower()
        assert ">format<" in response.text.lower()
        assert ">fields<" in response.text.lower()
        assert ">dest<" in response.text.lower()
        assert "Full collection" not in response.text

        settings = get_settings()
        expected_export = str(
            resolve_utility_directory(
                db_value="",
                default_parent=settings.data_dir,
                default_subdir="exports",
                library_root=settings.library_root,
                data_dir=settings.data_dir,
            )
        )
        assert f"exportFolderBrowsePath: {json.dumps(expected_export)}" in response.text
        assert (
            "Choose the output format, decide which metadata fields should be included"
            not in response.text
        )
        assert "External reporting" not in response.text
