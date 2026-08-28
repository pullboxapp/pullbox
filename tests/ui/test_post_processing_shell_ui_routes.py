"""Route-contract tests for the tabbed post-processing shell."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

import pullbox.ui.routes as ui_routes
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-post-processing-shell-ui")


async def _seed_post_processing_contract_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed queue and history rows so shared contracts can be asserted."""
    async with sec_db() as session:
        batman = Series(title="Batman", sort_title="batman")
        action = Series(title="Action Comics", sort_title="action comics")
        detective = Series(title="Detective Comics", sort_title="detective comics")
        session.add_all([batman, action, detective])
        await session.flush()

        batman_issue = Issue(series_id=batman.id, issue_number=1.0)
        action_issue = Issue(series_id=action.id, issue_number=50.0)
        detective_issue = Issue(series_id=detective.id, issue_number=3.0)
        session.add_all([batman_issue, action_issue, detective_issue])
        await session.flush()

        session.add_all(
            [
                DownloadHistory(
                    issue_id=batman_issue.id,
                    title="Batman 001 (2024) (Digital).cbz",
                    download_url="https://example.com/batman-001",
                    download_client=DownloadClientType.SABNZBD,
                    state=DownloadState.COMPLETED,
                    file_size=52_428_800,
                    downloaded_path="/downloads/Batman 001 (2024).cbz",
                    final_path="/library/Batman/Batman 001 (2024).cbz",
                    completed_at=datetime(2026, 4, 3, 18, 0, tzinfo=UTC),
                    imported_at=datetime(2026, 4, 3, 18, 5, tzinfo=UTC),
                    updated_at=datetime(2026, 4, 3, 18, 5, tzinfo=UTC),
                ),
                DownloadHistory(
                    issue_id=action_issue.id,
                    title="Action Comics 050 (2024) (Digital).cbz",
                    download_url="https://example.com/action-050",
                    download_client=DownloadClientType.TRANSMISSION,
                    state=DownloadState.FAILED,
                    file_size=83_886_080,
                    downloaded_path="/downloads/Action Comics 050 (2024).cbz",
                    error_message="Move failed: disk full",
                    completed_at=datetime(2026, 4, 3, 19, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 4, 3, 19, 15, tzinfo=UTC),
                ),
                DownloadHistory(
                    issue_id=detective_issue.id,
                    title="Detective Comics 003 (2024) (Digital).cbz",
                    download_url="https://example.com/detective-003",
                    download_client=DownloadClientType.QBITTORRENT,
                    state=DownloadState.COMPLETED,
                    file_size=41_943_040,
                    downloaded_path="/downloads/Detective Comics 003 (2024).cbz",
                    completed_at=datetime(2026, 4, 3, 20, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 4, 3, 20, 5, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
class TestPostProcessingRouteContracts:
    """Verify the post-processing page renders stable mounted regions."""

    async def test_post_processing_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/post-processing")

        assert response.status_code == 200
        assert 'data-testid="post-processing-page"' in response.text
        assert 'data-testid="post-processing-shell"' in response.text
        assert 'data-testid="post-processing-body"' in response.text
        assert 'data-testid="post-processing-tabs"' in response.text
        assert 'data-testid="post-processing-tab-queue"' in response.text
        assert 'data-testid="post-processing-tab-history"' in response.text
        assert 'data-testid="post-processing-content"' in response.text
        assert 'data-testid="post-processing-header"' in response.text
        assert 'data-testid="pp-gauges"' in response.text
        assert 'data-testid="post-processing-header-actions"' in response.text
        assert 'data-testid="pp-queue-panel"' in response.text
        assert 'data-testid="pp-footer-dock"' in response.text
        assert 'data-testid="pp-queue-active-section"' in response.text
        assert 'data-testid="pp-queue-imported-section"' in response.text
        assert 'data-testid="pp-history-panel"' not in response.text
        assert 'data-testid="pp-queue-empty"' in response.text
        assert 'data-testid="pp-queue-imported-empty"' in response.text
        assert "No active imports" in response.text

    async def test_post_processing_hx_query_returns_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/post-processing?tab=history&result=failed&search=Batman&sort=title",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="post-processing-content"' in response.text
        assert 'data-testid="post-processing-header"' in response.text
        assert 'data-testid="pp-gauges"' in response.text
        assert 'data-testid="pp-history-panel"' in response.text
        assert 'data-testid="pp-history-toolbar"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'hx-swap-oob="innerHTML"' in response.text
        assert 'data-testid="post-processing-page"' not in response.text

    async def test_post_processing_queue_partial_returns_panel_only(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/post-processing/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="post-processing-content"' in response.text
        assert 'data-testid="post-processing-header"' in response.text
        assert 'data-testid="pp-gauges"' in response.text
        assert 'data-testid="pp-queue-panel"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'data-testid="pp-footer-dock"' in response.text
        assert 'data-testid="pp-queue-active-section"' in response.text
        assert 'data-testid="pp-queue-imported-section"' in response.text
        assert 'data-testid="pp-queue-empty"' in response.text
        assert 'data-testid="pp-queue-imported-empty"' in response.text
        assert (
            'hx-trigger="every 2s [window.postProcessingQueueRefreshEnabled()], '
            'post-processing:refresh from:body"'
        ) in response.text
        assert 'data-testid="post-processing-page"' not in response.text
        assert 'data-testid="pp-history-panel"' not in response.text

    async def test_post_processing_history_partial_returns_panel_only(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/post-processing/history?result=failed&search=Batman&sort=title&page=1",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'id="pp-history-header-metrics"' in response.text
        assert 'id="pp-history-sort-input"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'data-testid="pp-history-results"' in response.text
        assert 'data-testid="pp-history-table"' not in response.text
        assert 'data-testid="pp-history-empty"' in response.text
        assert 'class="downloads-empty-state is-history"' in response.text
        assert 'data-testid="pp-history-toolbar"' not in response.text
        assert 'data-testid="post-processing-page"' not in response.text

    async def test_post_processing_history_pagination_scrolls_to_top_of_content(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            batman = Series(title="Batman", sort_title="batman")
            session.add(batman)
            await session.flush()

            issue = Issue(series_id=batman.id, issue_number=1.0)
            session.add(issue)
            await session.flush()

            session.add_all(
                [
                    DownloadHistory(
                        issue_id=issue.id,
                        title=f"Batman Import {idx:03d}.cbz",
                        download_url=f"https://example.com/batman-import-{idx}",
                        download_client=DownloadClientType.SABNZBD,
                        state=DownloadState.COMPLETED,
                        downloaded_path=f"/downloads/batman-import-{idx}.cbz",
                        completed_at=datetime(2026, 4, 3, 18, 0, tzinfo=UTC),
                        imported_at=datetime(2026, 4, 3, 18, 5, tzinfo=UTC),
                        updated_at=datetime(2026, 4, 3, 18, 5, tzinfo=UTC),
                    )
                    for idx in range(51)
                ]
            )
            await session.commit()

        response = await authenticated_client.get(
            "/htmx/post-processing/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="series-pagination-next"' in response.text
        assert 'hx-target="#pp-history-results"' in response.text
        assert 'hx-swap="outerHTML"' in response.text
        assert 'hx-sync="#pp-history-results:replace"' in response.text

    async def test_post_processing_history_partial_keeps_exact_poll_query_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/post-processing/history?result=failed&client=transmission&search=Archive&sort=title&page=2",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        match = re.search(
            r'<div[^>]+id="pp-history-results"[^>]+hx-get="([^"]+)"',
            response.text,
            re.S,
        )
        assert match is not None

        parsed = urlsplit(unescape(match.group(1)))
        assert parsed.path == "/htmx/post-processing/history"
        assert parse_qs(parsed.query) == {
            "page": ["1"],
            "result": ["failed"],
            "client": ["transmission"],
            "search": ["Archive"],
            "sort": ["title"],
        }

    async def test_post_processing_queue_populated_panel_uses_shared_queue_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/post-processing/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="pp-queue-panel"' in response.text
        assert 'data-testid="pp-queue-active-table"' in response.text
        assert response.text.count('class="downloads-table-wrap is-clipped"') >= 2
        assert 'data-testid="pp-queue-item"' in response.text
        assert 'data-testid="pp-queue-imported-section"' in response.text
        assert 'data-testid="pp-queue-item-details-toggle"' in response.text
        assert "/htmx/post-processing/queue/" in response.text
        assert "window.htmx.ajax('GET'" in response.text
        assert 'data-testid="pp-queue-item-detail-placeholder"' in response.text
        assert 'data-testid="pp-queue-item-detail-content"' not in response.text
        assert "Source path" not in response.text
        assert "Library path" not in response.text
        assert "Detective Comics 003 (2024) (Digital).cbz" in response.text
        assert "qBittorrent" in response.text
        assert 'aria-label="Toggle details"' in response.text
        assert 'hx-boost="false" class="downloads-issue-link"' in response.text
        assert 'data-testid="pp-queue-empty"' not in response.text

    async def test_post_processing_queue_detail_loads_only_on_expand(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        async with sec_db() as session:
            download_id = (
                await session.execute(
                    select(DownloadHistory.id).where(
                        DownloadHistory.title == "Detective Comics 003 (2024) (Digital).cbz"
                    )
                )
            ).scalar_one()

        response = await authenticated_client.get(
            f"/htmx/post-processing/queue/{download_id}/detail",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert f'id="pp-queue-detail-content-{download_id}"' in response.text
        assert 'data-testid="pp-queue-item-detail-content"' in response.text
        assert "Detective Comics #3" in response.text
        assert "Source path" in response.text
        assert "/downloads/Detective Comics 003 (2024).cbz" in response.text
        assert "Library path" in response.text
        assert "Pending library move" in response.text
        assert 'hx-boost="false"' in response.text

    async def test_post_processing_queue_partial_renders_phase_progress_and_time(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        async def _fake_live_status_map(session, active_items):  # type: ignore[no-untyped-def]
            del session
            assert len(active_items) == 1
            return {
                active_items[0].id: {
                    "phase_label": "Registering library file",
                    "status_label": "Registering library file",
                    "shows_transfer_metrics": False,
                    "elapsed_seconds": 14,
                    "phase_progress_pct": None,
                    "phase_progress_label": "In progress",
                    "progress_indeterminate": True,
                }
            }

        monkeypatch.setattr(
            ui_routes,
            "_load_post_processing_live_status_map",
            _fake_live_status_map,
        )

        response = await authenticated_client.get(
            "/htmx/post-processing/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Registering library file" in response.text
        assert "In progress" in response.text
        assert "14s elapsed" in response.text
        assert 'data-testid="pp-queue-item-progress-bar"' in response.text
        assert "is-indeterminate" in response.text
        assert "justify-center text-center leading-tight" in response.text
        assert 'aria-label="Toggle details"' in response.text

    async def test_post_processing_queue_partial_renders_transfer_telemetry(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        async def _fake_live_status_map(session, active_items):  # type: ignore[no-untyped-def]
            del session
            assert len(active_items) == 1
            return {
                active_items[0].id: {
                    "phase_label": "Transferring file",
                    "status_label": "Transferring",
                    "shows_transfer_metrics": True,
                    "elapsed_seconds": 18,
                    "transfer_progress_pct": 62.5,
                    "transfer_done_bytes": 1_342_177_280,
                    "transfer_total_bytes": 2_147_483_648,
                    "transfer_speed_bytes": 52_428_800,
                    "transfer_eta_seconds": 15,
                }
            }

        monkeypatch.setattr(
            ui_routes,
            "_load_post_processing_live_status_map",
            _fake_live_status_map,
        )

        response = await authenticated_client.get(
            "/htmx/post-processing/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="pp-queue-item-progress-bar"' in response.text
        assert 'style="width: 62.5%"' in response.text
        assert "62% · 1.2 GB / 2.0 GB" in response.text
        assert "50.0 MB/s" in response.text
        assert "15s left" in response.text
        assert ">Status<" in response.text
        assert ">Progress<" in response.text
        assert ">Time<" in response.text
        assert "Transferring" in response.text
        assert "Transferring file" not in response.text
        assert 'data-testid="pp-queue-item-time"' in response.text

    async def test_recently_imported_item_lingers_in_queue_with_completion_state(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        async with sec_db() as session:
            imported_item = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.title == "Batman 001 (2024) (Digital).cbz"
                    )
                )
            ).scalar_one()

        monkeypatch.setattr(
            ui_routes,
            "_get_recent_post_processing_completion_ids",
            lambda: {imported_item.id},
        )

        async def _fake_live_status_map(session, active_items):  # type: ignore[no-untyped-def]
            del session
            return {
                item.id: {
                    "phase_label": "Import complete",
                    "status_label": "Import complete",
                    "shows_transfer_metrics": False,
                    "elapsed_seconds": 22,
                    "state_tone": "success",
                    "transfer_progress_pct": 100.0 if item.id == imported_item.id else None,
                    "transfer_done_bytes": 52_428_800 if item.id == imported_item.id else None,
                    "transfer_total_bytes": 52_428_800 if item.id == imported_item.id else None,
                    "transfer_speed_bytes": None,
                    "transfer_eta_seconds": 0 if item.id == imported_item.id else None,
                }
                for item in active_items
            }

        monkeypatch.setattr(
            ui_routes,
            "_load_post_processing_live_status_map",
            _fake_live_status_map,
        )

        response = await authenticated_client.get(
            "/htmx/post-processing/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Batman 001 (2024) (Digital).cbz" in response.text
        assert 'data-testid="pp-queue-imported-table"' in response.text
        assert "Imported" in response.text
        assert "pill-success" in response.text
        assert 'hx-boost="false" class="downloads-issue-link"' in response.text
        assert 'data-testid="pp-queue-item-progress-bar"' not in response.text

    async def test_post_processing_live_status_map_uses_real_snapshot_phase_progress(
        self,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.operation_progress import (
            OperationProgressState,
            OperationProgressType,
        )
        from pullbox.services.operation_progress import (
            OperationProgressUpdate,
            publish_operation_progress,
        )

        async with sec_db() as session:
            series = Series(title="Live Snapshot", sort_title="live snapshot")
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=3.0)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                issue_id=issue.id,
                title="Live Snapshot.cbz",
                download_url="https://example.com/live-snapshot",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.COMPLETED,
                downloaded_path="/downloads/live-snapshot.cbz",
                updated_at=datetime(2026, 4, 3, 20, 5, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()
            await publish_operation_progress(
                session,
                OperationProgressUpdate(
                    operation_type=OperationProgressType.POST_PROCESSING,
                    operation_key=str(download.id),
                    revision=2,
                    state=OperationProgressState.RUNNING,
                    phase="validating_files",
                    title=download.title,
                    message="Validating files",
                ),
            )
            await session.commit()

        async with sec_db() as session:
            live_status = await ui_routes._load_post_processing_live_status_map(
                session,
                [download],
            )

        assert live_status[download.id]["phase_label"] == "Validating files"
        assert live_status[download.id]["phase_progress_pct"] is None
        assert live_status[download.id]["phase_progress_label"] == "In progress"
        assert live_status[download.id]["progress_indeterminate"] is True
        assert live_status[download.id]["shows_transfer_metrics"] is False

    async def test_recently_imported_item_is_hidden_from_history_until_queue_grace_ends(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        async with sec_db() as session:
            imported_item = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.title == "Batman 001 (2024) (Digital).cbz"
                    )
                )
            ).scalar_one()

        monkeypatch.setattr(
            ui_routes,
            "_get_recent_post_processing_completion_ids",
            lambda: {imported_item.id},
        )

        response = await authenticated_client.get(
            "/htmx/post-processing/history?result=imported",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Batman 001 (2024) (Digital).cbz" not in response.text

    async def test_post_processing_history_populated_panel_uses_shared_contracts(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_contract_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/post-processing/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="pp-history-table"' in response.text
        assert 'id="pp-history-header-metrics"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'class="downloads-table"' in response.text
        assert 'data-testid="pp-history-sort-title"' in response.text
        assert 'data-testid="pp-history-sort-issue"' in response.text
        assert 'data-testid="pp-history-sort-result"' in response.text
        assert 'data-testid="pp-history-sort-client"' in response.text
        assert 'data-testid="pp-history-sort-size"' in response.text
        assert 'data-testid="pp-history-sort-completed_at"' in response.text
        assert 'data-testid="pp-history-remove-' in response.text
        assert 'id="pp-history-sort-input"' in response.text
        assert 'value="-completed_at"' in response.text
        assert "tooltip-wrap" in response.text
        assert "data-tooltip-auto" in response.text
        assert "data-tooltip-measure" in response.text
        assert 'hx-boost="false" class="downloads-issue-link"' in response.text
