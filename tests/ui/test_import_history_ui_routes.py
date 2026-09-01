"""Route-contract tests for the unified Import history tab."""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-history-ui")


async def _seed_import_history_job(
    sec_db,
    *,
    status: str = "scanning",
    source_path: str = "/tmp/import-history-contract",
    source_type: str = "filesystem",
    progress_snapshot: dict[str, object] | None = None,
) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType

    async with sec_db() as session:
        job = ImportJob(
            source_path=source_path,
            source_type=ImportSourceType(source_type),
            status=ImportJobStatus(status),
            series_found=7,
            series_imported=0,
            series_failed=0,
            series_no_match=2,
            progress_snapshot=progress_snapshot or {},
        )
        session.add(job)
        await session.commit()
        return job.id


@pytest.mark.asyncio
class TestImportHistoryTabRouteContracts:
    """Verify history is mounted under the unified Import workspace."""

    async def test_legacy_import_history_route_redirects_to_unified_import_page(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import/history")

        assert response.status_code == 307
        assert response.headers["location"] == "/import?tab=history"

    async def test_import_history_renders_inside_import_workspace(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert 'data-testid="import-page"' in response.text
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-header-gauges-spacer"' in response.text
        assert 'data-testid="import-header-gauges"' not in response.text
        assert 'data-testid="import-tabs"' in response.text
        assert 'data-testid="import-footer-dock"' in response.text
        assert 'data-testid="import-history-page"' in response.text
        assert 'data-testid="import-history-toolbar"' in response.text
        assert 'data-testid="import-history-search"' in response.text
        assert 'data-testid="import-history-results"' in response.text
        assert 'data-testid="import-history-table-shell"' in response.text
        assert 'data-testid="import-history-delete-modal"' in response.text

    @pytest.mark.parametrize("source_type", ["filesystem", "mylar3"])
    async def test_step_four_explains_that_import_continues_in_background(
        self,
        authenticated_client,
        sec_db,
        source_type: str,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_history_job(
            sec_db,
            status="importing",
            source_path=("/tmp/mylar.db" if source_type == "mylar3" else "/tmp/comics"),
            source_type=source_type,
        )

        response = await authenticated_client.get(
            f"/import/{job_id}/progress-partial?next_step=5&mode=import"
        )

        assert response.status_code == 200
        assert 'data-testid="import-background-notice"' in response.text
        assert "You can safely leave this page" in response.text
        assert 'data-testid="import-background-library-link"' in response.text
        assert 'data-testid="import-background-dashboard-link"' in response.text

    async def test_import_history_inner_hx_request_returns_history_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/import?tab=history",
            headers={"HX-Request": "true", "HX-Target": "import-history-results"},
        )

        assert response.status_code == 200
        assert 'data-testid="import-header"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'data-testid="import-history-results"' in response.text
        assert 'id="import-history-page-input"' in response.text
        assert 'id="import-history-sort-input"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="import-content"' not in response.text
        assert 'data-testid="import-history-page"' not in response.text

    async def test_import_history_panel_hx_request_returns_toolbar_and_results(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db)
        response = await authenticated_client.get(
            "/import?tab=history",
            headers={"HX-Request": "true", "HX-Target": "import-history-page"},
        )

        assert response.status_code == 200
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-history-page"' in response.text
        assert 'data-testid="import-history-toolbar"' in response.text
        assert 'data-testid="import-history-results"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text

    async def test_import_history_table_uses_shared_history_table_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="failed")
        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert 'data-testid="import-history-table"' in response.text
        assert 'class="downloads-led ' in response.text
        assert 'class="downloads-action-btn import-history-action-btn' in response.text
        assert 'class="downloads-error-row table-detail-row"' in response.text

    async def test_import_history_labels_incomplete_rollback_truthfully(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(
            sec_db,
            status="failed",
            progress_snapshot={
                "mode": "rollback",
                "phase": "rollback_incomplete",
                "rollback_manual_recovery_count": 1,
            },
        )

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert "Rollback Incomplete" in response.text

    async def test_import_history_toolbar_matches_downloads_history_search_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="failed")
        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert 'id="import-history-filter-form"' in response.text
        assert 'data-testid="import-history-toolbar"' in response.text
        assert 'hx-target="#import-history-results"' in response.text
        assert 'hx-sync="#import-history-results:replace"' in response.text
        assert 'data-testid="import-history-search-field"' in response.text
        assert 'data-search-field-mode="remote"' in response.text
        assert 'data-search-field-debounce="250"' in response.text
        assert 'data-testid="import-history-clear"' in response.text
        assert 'id="import-history-sort-input"' in response.text

    async def test_import_history_table_headers_use_shared_sort_button_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="failed")
        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert 'data-testid="import-history-sort-source_path"' in response.text
        assert 'data-testid="import-history-sort-created_at"' in response.text
        assert 'class="downloads-sort-btn' in response.text
        assert 'class="downloads-sort-chevron' in response.text

    async def test_import_history_uses_true_paginated_history_rows(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        for idx in range(30):
            await _seed_import_history_job(
                sec_db,
                status="failed",
                source_path=f"/tmp/import-history-page-{idx:02d}",
            )

        response = await authenticated_client.get("/import?tab=history&sort=source_path&page=2")

        assert response.status_code == 200
        assert "/tmp/import-history-page-25" in response.text
        assert "/tmp/import-history-page-29" in response.text
        assert "/tmp/import-history-page-00" not in response.text
        assert 'value="2"' in response.text

    async def test_import_history_footer_pagination_preserves_sort_and_search(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        for idx in range(30):
            await _seed_import_history_job(
                sec_db,
                status="failed",
                source_path=f"/tmp/import-history-page-{idx:02d}",
            )

        response = await authenticated_client.get(
            "/import?tab=history&search=page&sort=source_path&page=2"
        )

        assert response.status_code == 200
        assert 'data-testid="page-dock-pagination"' in response.text
        assert (
            'data-page-url="/import?tab=history&amp;search=page&amp;sort=source_path&amp;page=1"'
            in response.text
        )
        assert (
            'hx-get="/import?tab=history&amp;search=page&amp;sort=source_path&amp;page=1"'
            in response.text
        )
        assert 'hx-sync="#import-history-results:replace"' in response.text
        assert "&quot;#import-history-results:replace&quot;" not in response.text
        assert "&#34;#import-history-results:replace&#34;" not in response.text

    async def test_import_history_sort_query_marks_active_header_and_preserves_results_bundle(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="failed")
        response = await authenticated_client.get("/import?tab=history&sort=source_path")

        assert response.status_code == 200
        assert 'data-testid="import-history-sort-source_path"' in response.text
        assert 'class="downloads-sort-btn is-active"' in response.text

    async def test_import_history_search_filters_jobs_by_source_path(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db)
        response = await authenticated_client.get("/import?tab=history&search=contract")

        assert response.status_code == 200
        assert "/tmp/import-history-contract" in response.text
        assert 'data-testid="import-history-table"' in response.text

    async def test_import_history_results_poll_while_live_jobs_are_present(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="rolling_back")

        response = await authenticated_client.get("/import?tab=history&page=2")

        assert response.status_code == 200
        assert (
            'hx-trigger="every 1s [window.importHistoryRefreshEnabled()], refresh"' in response.text
        )
        assert 'hx-get="/import?tab=history&amp;sort=-created_at&amp;page=1"' in response.text
        assert 'hx-push-url="false"' in response.text

    async def test_import_history_summary_reflects_real_row_actions(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_import_history_job(sec_db, status="rolling_back")
        await _seed_import_history_job(sec_db, status="paused")
        await _seed_import_history_job(sec_db, status="completed")
        await _seed_import_history_job(sec_db, status="failed")

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert "4 jobs · 1 active · 1 resumable · 2 results ready" in response.text
        assert ">resumable<" in response.text
        assert ">results<" in response.text
        assert ">follow-up<" not in response.text

    @pytest.mark.parametrize(
        ("status", "expects_delete"),
        [
            ("pending", True),
            ("scanning", False),
            ("paused", True),
            ("analyzing", False),
            ("matching", False),
            ("file_matching", False),
            ("review", True),
            ("importing", False),
            ("cancelling", False),
            ("rolling_back", False),
            ("completed", False),
            ("failed", True),
            ("cancelled", True),
            ("rolled_back", True),
        ],
    )
    async def test_import_history_delete_action_matrix_matches_status_contract(
        self,
        authenticated_client,
        sec_db,
        status: str,
        expects_delete: bool,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_history_job(sec_db, status=status)

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        marker = f'data-testid="import-history-delete-{job_id}"'
        if expects_delete:
            assert marker in response.text
        else:
            assert marker not in response.text

    async def test_import_history_uses_restored_series_counts_for_cancelled_jobs(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import (
            ImportedSeries,
            ImportJob,
            ImportJobStatus,
            ImportSeriesStatus,
            ImportSourceType,
        )

        async with sec_db() as session:
            job = ImportJob(
                source_path="/tmp/import-history-cancelled-counts",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.CANCELLED,
                series_found=39,
                series_imported=99,
                series_failed=12,
                series_no_match=7,
            )
            session.add(job)
            await session.flush()
            session.add_all(
                [
                    ImportedSeries(
                        import_job_id=job.id,
                        raw_series_name="Recovered Match",
                        status=ImportSeriesStatus.MATCHED,
                        file_count=1,
                    ),
                    ImportedSeries(
                        import_job_id=job.id,
                        raw_series_name="Library Duplicate",
                        status=ImportSeriesStatus.DUPLICATE,
                        file_count=1,
                    ),
                    ImportedSeries(
                        import_job_id=job.id,
                        raw_series_name="Still No Match",
                        status=ImportSeriesStatus.NO_MATCH,
                        file_count=1,
                    ),
                ]
            )
            await session.commit()
            job_id = job.id

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        row_match = re.search(
            rf'<tr\s+id="import-job-row-{job_id}"[^>]*>(.*?)</tr>',
            response.text,
            re.DOTALL,
        )
        assert row_match is not None
        cell_text = [
            re.sub(r"<[^>]+>", " ", cell).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_match.group(0), re.DOTALL)
        ]

        assert cell_text[4] == "3"
        assert cell_text[5] == "0"
        assert cell_text[6] == "0"
        assert cell_text[7] == "1"
