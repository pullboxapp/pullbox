"""Route-contract tests for the rewritten downloads shell."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

import pullbox.ui.routes as ui_routes
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.operation_progress import (
    OperationProgressState,
    OperationProgressType,
)
from pullbox.models.series import Series
from pullbox.services.operation_progress import (
    OperationProgressMeasure,
    OperationProgressUpdate,
    publish_operation_progress,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-downloads-ui")

_ACTIVE_EMPTY_ICON_PATH = (
    'd="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5'
    'M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z"'
)
_WAITING_EMPTY_ICON_PATH = 'd="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z"'


async def _seed_download_history_contract_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed configured clients plus a couple of history rows for UI contract tests."""
    async with sec_db() as session:
        session.add_all(
            [
                DownloadClientConfig(
                    name="Main Usenet",
                    client_type=DownloadClientType.SABNZBD,
                    url="http://localhost:8080",
                    enabled=True,
                    priority=10,
                    api_key="test-key",
                ),
                DownloadClientConfig(
                    name="Archive Torrent",
                    client_type=DownloadClientType.TRANSMISSION,
                    url="http://localhost:9091",
                    enabled=True,
                    priority=20,
                    username="pullbox",
                    password="secret",
                ),
            ]
        )
        batman = Series(title="Batman", sort_title="batman")
        action = Series(title="Action Comics", sort_title="action comics")
        session.add_all([batman, action])
        await session.flush()

        issue_one = Issue(series_id=batman.id, issue_number=1.0)
        issue_two = Issue(series_id=batman.id, issue_number=2.0)
        issue_three = Issue(series_id=action.id, issue_number=50.0)
        session.add_all([issue_one, issue_two, issue_three])
        await session.flush()

        session.add_all(
            [
                DownloadHistory(
                    title="Batman 001 (2024) (Digital).cbz",
                    state=DownloadState.FAILED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/batman-001.nzb",
                    issue_id=issue_one.id,
                    error_message="Connection refused",
                    file_size=30_000_000,
                    updated_at=datetime(2026, 4, 3, 18, 30, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Action Comics 050 (2024) (Digital).cbz",
                    state=DownloadState.COMPLETED,
                    download_client=DownloadClientType.TRANSMISSION,
                    download_url="https://example.com/action-050.torrent",
                    issue_id=issue_three.id,
                    file_size=55_000_000,
                    updated_at=datetime(2026, 4, 3, 19, 15, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Batman 002 (2024) (Digital).cbz",
                    state=DownloadState.FAILED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/batman-002.nzb",
                    issue_id=issue_two.id,
                    error_message="Cancelled by user",
                    file_size=80_000_000,
                    updated_at=datetime(2026, 4, 3, 17, 45, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Should Not Appear Imported.cbz",
                    state=DownloadState.COMPLETED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/imported.nzb",
                    issue_id=issue_one.id,
                    downloaded_path="/downloads/imported.cbz",
                    imported_at=datetime(2026, 4, 3, 20, 10, tzinfo=UTC),
                    file_size=42_000_000,
                    updated_at=datetime(2026, 4, 3, 20, 10, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Should Not Appear Processing Failure.cbz",
                    state=DownloadState.FAILED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/pp-failed.nzb",
                    issue_id=issue_two.id,
                    downloaded_path="/downloads/pp-failed.cbz",
                    error_message="Move failed: disk full",
                    file_size=44_000_000,
                    updated_at=datetime(2026, 4, 3, 20, 20, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()


async def _seed_post_processing_only_history_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed successful/processing-failed rows that do not belong to Downloads History."""
    async with sec_db() as session:
        series = Series(title="Wolverine", sort_title="wolverine")
        session.add(series)
        await session.flush()

        issue_one = Issue(series_id=series.id, issue_number=1.0)
        issue_two = Issue(series_id=series.id, issue_number=2.0)
        session.add_all([issue_one, issue_two])
        await session.flush()

        session.add_all(
            [
                DownloadHistory(
                    title="Wolverine 001 (2026) (Digital).cbz",
                    state=DownloadState.COMPLETED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/wolverine-001.nzb",
                    issue_id=issue_one.id,
                    downloaded_path="/downloads/wolverine-001.cbz",
                    final_path="/comics/Wolverine/Wolverine 001.cbz",
                    imported_at=datetime(2026, 4, 3, 20, 10, tzinfo=UTC),
                    file_size=42_000_000,
                    updated_at=datetime(2026, 4, 3, 20, 10, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Wolverine 002 (2026) (Digital).cbz",
                    state=DownloadState.FAILED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/wolverine-002.nzb",
                    issue_id=issue_two.id,
                    downloaded_path="/downloads/wolverine-002.cbz",
                    error_message="Move failed: destination is not writable",
                    file_size=43_000_000,
                    updated_at=datetime(2026, 4, 3, 20, 20, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()


async def _seed_download_queue_contract_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed an active queue row so the shared queue card contract can be asserted."""
    async with sec_db() as session:
        batman = Series(title="Batman", sort_title="batman")
        session.add(batman)
        await session.flush()

        issue_two = Issue(series_id=batman.id, issue_number=2.0)
        session.add(issue_two)
        await session.flush()

        session.add(
            DownloadHistory(
                title="Batman 002 (2024) (Digital).cbz",
                state=DownloadState.DOWNLOADING,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/batman-002.nzb",
                external_id="queue-contract-download",
                issue_id=issue_two.id,
                file_size=104_857_600,
                sent_at=datetime(2026, 4, 3, 17, 0, tzinfo=UTC),
                updated_at=datetime(2026, 4, 3, 17, 5, tzinfo=UTC),
            )
        )
        session.add(
            DownloadHistory(
                title="Batman 003 (2024) (Digital).cbz",
                state=DownloadState.QUEUED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/batman-003.nzb",
                issue_id=issue_two.id,
                file_size=88_000_000,
                sent_at=datetime(2026, 4, 3, 17, 6, tzinfo=UTC),
                updated_at=datetime(2026, 4, 3, 17, 6, tzinfo=UTC),
            )
        )
        await session.commit()


async def _seed_multi_active_download_queue_data(sec_db) -> None:  # type: ignore[no-untyped-def]
    """Seed two active queue rows on the same client for batching tests."""
    async with sec_db() as session:
        detective = Series(title="Detective Comics", sort_title="detective comics")
        session.add(detective)
        await session.flush()

        issue = Issue(series_id=detective.id, issue_number=27.0)
        session.add(issue)
        await session.flush()

        session.add_all(
            [
                DownloadHistory(
                    title="Detective Comics 027 (2024) (Digital).cbz",
                    state=DownloadState.DOWNLOADING,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/detective-027.nzb",
                    external_id="queue-batch-a",
                    issue_id=issue.id,
                    file_size=90_000_000,
                    sent_at=datetime(2026, 4, 3, 18, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 4, 3, 18, 1, tzinfo=UTC),
                ),
                DownloadHistory(
                    title="Detective Comics 028 (2024) (Digital).cbz",
                    state=DownloadState.DOWNLOADING,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/detective-028.nzb",
                    external_id="queue-batch-b",
                    issue_id=issue.id,
                    file_size=95_000_000,
                    sent_at=datetime(2026, 4, 3, 18, 2, tzinfo=UTC),
                    updated_at=datetime(2026, 4, 3, 18, 3, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
class TestDownloadsRouteContracts:
    """Verify the downloads page renders stable shell and HTMX partials."""

    async def test_downloads_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/downloads")

        assert response.status_code == 200
        assert 'data-testid="downloads-page"' in response.text
        assert 'data-testid="downloads-body"' in response.text
        assert 'data-testid="downloads-content"' in response.text
        assert 'data-testid="downloads-header"' in response.text
        assert 'data-testid="downloads-gauges"' in response.text
        assert 'data-testid="downloads-tabs"' in response.text
        assert 'data-testid="downloads-header-actions"' in response.text
        assert 'data-testid="downloads-tab-queue"' in response.text
        assert 'data-testid="downloads-tab-history"' in response.text
        assert 'data-testid="downloads-queue-panel"' in response.text
        assert 'data-testid="downloads-footer-dock"' in response.text
        assert 'data-testid="downloads-queue-active-section"' in response.text
        assert 'data-testid="downloads-queue-waiting-section"' in response.text
        assert 'data-testid="downloads-queue-active-empty"' in response.text
        assert 'data-testid="downloads-queue-waiting-empty"' in response.text
        assert _ACTIVE_EMPTY_ICON_PATH in response.text
        assert _WAITING_EMPTY_ICON_PATH in response.text
        assert 'data-testid="downloads-summary-cards"' not in response.text
        assert 'data-testid="downloads-footer-strip"' not in response.text
        assert 'data-testid="downloads-source-modal"' in response.text
        assert 'aria-labelledby="downloads-source-modal-title"' in response.text
        assert "Already transferred data will be discarded" in response.text
        assert (
            response.text.index('data-testid="downloads-header-actions"')
            < response.text.index('data-testid="downloads-tabs"')
            < response.text.index('data-testid="downloads-queue-panel"')
        )

    async def test_downloads_hx_tab_switch_returns_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/downloads?tab=history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-content"' in response.text
        assert 'data-testid="downloads-header"' in response.text
        assert 'data-testid="downloads-gauges"' in response.text
        assert 'data-testid="downloads-history-panel"' in response.text
        assert 'data-testid="downloads-history-toolbar"' in response.text
        assert 'data-testid="downloads-footer-dock"' in response.text
        assert "Completed" in response.text
        assert "Failed" in response.text
        assert "Cancelled" in response.text
        assert 'data-testid="downloads-page"' not in response.text
        assert (
            response.text.index('data-testid="downloads-header-actions"')
            < response.text.index('data-testid="downloads-tabs"')
            < response.text.index('data-testid="downloads-history-panel"')
        )

    async def test_download_queue_partial_returns_panel_only(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/downloads/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-content"' in response.text
        assert 'data-testid="downloads-header"' in response.text
        assert 'data-testid="downloads-gauges"' in response.text
        assert 'data-testid="downloads-queue-panel"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'data-testid="downloads-footer-dock"' in response.text
        assert 'data-testid="downloads-queue-active-section"' in response.text
        assert 'data-testid="downloads-queue-waiting-section"' in response.text
        assert 'data-testid="downloads-queue-active-empty"' in response.text
        assert 'data-testid="downloads-queue-waiting-empty"' in response.text
        assert _ACTIVE_EMPTY_ICON_PATH in response.text
        assert _WAITING_EMPTY_ICON_PATH in response.text
        assert (
            'hx-trigger="every 2s [window.pullboxLiveUpdatesEnabled()], '
            'downloads:refresh from:body"'
        ) in response.text
        assert 'data-testid="downloads-page"' not in response.text
        assert 'data-testid="downloads-footer-strip"' not in response.text

    async def test_download_queue_populated_panel_uses_shared_card_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_queue_contract_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/downloads/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-queue-panel"' in response.text
        assert 'data-testid="downloads-queue-active-table"' in response.text
        assert 'data-testid="downloads-queue-waiting-table"' in response.text
        assert response.text.count('class="downloads-table-wrap is-clipped"') >= 2
        assert 'data-testid="downloads-footer-dock"' in response.text
        assert 'data-testid="downloads-queue-item"' in response.text
        assert ">Status<" in response.text
        assert 'data-testid="downloads-queue-item-status"' in response.text
        assert "Active" in response.text
        assert "Queued" in response.text
        assert 'class="downloads-release-name tooltip-wrap"' in response.text
        assert response.text.count('hx-boost="false" class="downloads-issue-link"') == 2
        assert "No linked issue" not in response.text
        assert "SABnzbd" in response.text
        assert "Details" not in response.text
        assert 'data-testid="downloads-queue-active-empty"' not in response.text
        assert 'data-testid="downloads-queue-waiting-empty"' not in response.text

    async def test_download_queue_partial_keeps_live_progress_in_table_contract(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_queue_contract_data(sec_db)

        async def _fake_progress_map(session, queue_items, *, fallback_progress):  # type: ignore[no-untyped-def]
            del session
            del fallback_progress
            assert len(queue_items) == 2
            active_item = next(
                item for item in queue_items if item.state == DownloadState.DOWNLOADING
            )
            return {
                active_item.id: SimpleNamespace(
                    progress=0.67,
                    speed_bytes=2_097_152,
                    eta_seconds=75,
                    size_bytes=104_857_600,
                    updated_at=123.0,
                    client_state="Downloading",
                )
            }

        monkeypatch.setattr(ui_routes, "_load_download_progress_map", _fake_progress_map)

        response = await authenticated_client.get(
            "/htmx/downloads/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'style="width: 67.0%"' in response.text
        assert "67%" in response.text
        assert response.headers["cache-control"].startswith("no-store")

    async def test_download_queue_partial_animates_unknown_total_direct_transfer(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_queue_contract_data(sec_db)

        async def _fake_progress_map(session, queue_items, *, fallback_progress):  # type: ignore[no-untyped-def]
            del session
            del fallback_progress
            active_item = next(
                item for item in queue_items if item.state == DownloadState.DOWNLOADING
            )
            active_item.download_client = DownloadClientType.DIRECT
            return {
                active_item.id: SimpleNamespace(
                    progress=0.0,
                    speed_bytes=1_048_576,
                    eta_seconds=None,
                    size_bytes=None,
                    bytes_transferred=43_492_898,
                    is_indeterminate=True,
                    updated_at=123.0,
                    client_state="Downloading from TeraBox",
                    source_label="GetComics via TeraBox",
                )
            }

        monkeypatch.setattr(ui_routes, "_load_download_progress_map", _fake_progress_map)

        response = await authenticated_client.get(
            "/htmx/downloads/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "downloads-progress-fill is-blue is-indeterminate" in response.text
        assert 'style="width: 45%"' in response.text
        assert "41.5 MB received" in response.text
        assert "Downloading from TeraBox" in response.text
        assert 'data-testid="downloads-queue-try-next-source-' in response.text
        assert 'data-testid="downloads-queue-choose-source-' in response.text
        assert (
            'data-testid="downloads-queue-item-progress-label">41.5 MB received</span>'
            in response.text
        )

    async def test_download_queue_partial_surfaces_client_finalization_phase(
        self,
        authenticated_client,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_queue_contract_data(sec_db)

        async def _fake_progress_map(session, queue_items, *, fallback_progress):  # type: ignore[no-untyped-def]
            del session
            del fallback_progress
            assert len(queue_items) == 2
            active_item = next(
                item for item in queue_items if item.state == DownloadState.DOWNLOADING
            )
            return {
                active_item.id: SimpleNamespace(
                    progress=1.0,
                    speed_bytes=None,
                    eta_seconds=None,
                    size_bytes=104_857_600,
                    updated_at=123.0,
                    client_state="Repairing",
                )
            }

        monkeypatch.setattr(ui_routes, "_load_download_progress_map", _fake_progress_map)

        response = await authenticated_client.get(
            "/htmx/downloads/queue",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'style="width: 100.0%"' in response.text
        assert "Finalizing in client" in response.text
        assert "Repairing" in response.text
        assert 'data-testid="downloads-queue-item-status"' in response.text

    async def test_download_history_partial_returns_panel_only(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/downloads/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-history-results"' in response.text
        assert 'data-testid="downloads-history-table"' not in response.text
        assert 'data-testid="downloads-history-empty"' in response.text
        assert 'data-testid="downloads-history-toolbar"' not in response.text
        assert 'data-testid="downloads-content"' not in response.text
        assert 'data-testid="downloads-header"' not in response.text
        assert 'data-testid="downloads-page"' not in response.text
        assert 'id="downloads-history-header-metrics"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'data-testid="downloads-footer-dock"' in response.text
        assert (
            'hx-trigger="every 3s [window.downloadsHistoryRefreshEnabled()], refresh"'
            in response.text
        )
        assert 'class="downloads-pagination"' not in response.text
        assert 'data-testid="downloads-footer-strip"' not in response.text
        assert 'stroke-dashoffset="113.1"' in response.text

    async def test_download_history_populated_panel_uses_shared_contracts(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_history_contract_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/downloads/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-history-results"' in response.text
        assert 'data-testid="downloads-history-table"' in response.text
        assert ">SABnzbd<" in response.text
        assert ">Transmission<" in response.text
        assert "tooltip-wrap" in response.text
        assert "data-tooltip-auto" in response.text
        assert "data-tooltip-measure" in response.text
        assert 'hx-boost="false" class="downloads-issue-link"' in response.text
        assert 'data-testid="downloads-history-sort-title"' in response.text
        assert 'data-testid="downloads-history-sort-issue"' in response.text
        assert 'data-testid="downloads-history-sort-status"' in response.text
        assert 'data-testid="downloads-history-sort-client"' in response.text
        assert 'data-testid="downloads-history-sort-size"' in response.text
        assert 'data-testid="downloads-history-sort-updated_at"' in response.text
        assert 'id="downloads-history-sort-input"' in response.text
        assert 'hx-get="/htmx/downloads/history/' in response.text
        assert "/error-detail" in response.text
        assert 'aria-expanded="false"' in response.text
        assert 'hx-trigger="pullbox-download-history-detail-' in response.text
        assert "pbToggleLazyTableDetail($el, detailRowId," in response.text
        assert "detailRowId: 'downloads-history-error-row-" in response.text
        assert "pullbox-download-history-detail-" in response.text
        assert 'data-testid="downloads-history-error-detail-content"' not in response.text
        assert 'id="downloads-history-error-row-' not in response.text
        assert "Connection refused" not in response.text
        assert response.text.count('data-testid="downloads-history-block-') == 1
        assert 'class="downloads-error-row table-detail-row"' not in response.text
        assert 'id="downloads-history-header-metrics"' in response.text
        assert "Completed" in response.text
        assert "Done" not in response.text
        assert "Should Not Appear Imported.cbz" not in response.text
        assert "Should Not Appear Processing Failure.cbz" not in response.text

    async def test_download_history_client_includes_direct_artifact_host(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            series = Series(title="Murder Drones", sort_title="murder drones")
            session.add(series)
            await session.flush()
            issue = Issue(series_id=series.id, issue_number=4.0)
            session.add(issue)
            await session.flush()
            attempt = DirectAcquisitionAttempt(
                request_key="downloads-history:direct:datanodes",
                issue_id=issue.id,
                provider_identity="pullbox.getcomics",
                provider_candidate_id="candidate-datanodes",
                state=DirectAcquisitionState.FAILED,
                candidate_snapshot={"display_title": "Murder Drones 004 (2026)"},
            )
            attempt.artifact_attempts = [
                DirectArtifactAttempt(
                    sequence_no=0,
                    artifact_identity="datanodes-artifact",
                    route_kind=DirectArtifactRouteKind.DIRECT,
                    host_kind=DirectArtifactHostKind.DATANODES,
                    state=DirectArtifactState.FAILED,
                    is_selected=True,
                )
            ]
            session.add(attempt)
            await session.flush()
            session.add(
                DownloadHistory(
                    issue_id=issue.id,
                    title="Murder Drones 004 (2026)",
                    download_url=f"pullbox-direct://attempt/{attempt.id}",
                    download_client=DownloadClientType.DIRECT,
                    external_id=f"direct:{attempt.id}",
                    state=DownloadState.FAILED,
                    error_message="Test failure",
                )
            )
            await session.commit()

        response = await authenticated_client.get(
            "/htmx/downloads/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Direct Download · DataNodes" in response.text

    async def test_download_history_error_detail_loads_only_on_expand(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_history_contract_data(sec_db)

        async with sec_db() as session:
            download_id = (
                await session.execute(
                    select(DownloadHistory.id).where(
                        DownloadHistory.error_message == "Connection refused"
                    )
                )
            ).scalar_one()

        response = await authenticated_client.get(
            f"/htmx/downloads/history/{download_id}/error-detail",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert f'id="downloads-history-error-row-{download_id}"' in response.text
        assert "Connection refused" in response.text
        assert 'class="downloads-error-row table-detail-row"' in response.text
        assert 'data-testid="downloads-history-error-detail-content"' in response.text

    async def test_download_history_shell_uses_series_toolbar_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/downloads?tab=history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-history-toolbar"' in response.text
        assert 'data-testid="downloads-history-filter-status"' in response.text
        assert 'data-testid="downloads-history-filter-client"' in response.text
        assert 'data-testid="downloads-history-search"' in response.text
        assert 'data-testid="downloads-history-search-history-panel"' in response.text
        assert 'data-search-history-key="pullbox.searchHistory.downloads"' in response.text
        assert "data-search-field" in response.text
        assert "data-dropdown-select" in response.text
        assert 'class="series-toolbar-frame downloads-history-toolbar"' in response.text
        assert (
            'hx-trigger="submit, input delay:250ms from:[data-search-field-input]"'
            not in response.text
        )
        assert 'hx-target="#downloads-history-results"' in response.text
        assert 'hx-swap="outerHTML"' in response.text
        assert 'hx-sync="#downloads-history-results:replace"' in response.text

    async def test_download_history_shell_uses_clear_history_label_when_history_exists(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_history_contract_data(sec_db)

        response = await authenticated_client.get(
            "/downloads?tab=history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Clear History" in response.text
        assert ">Clear<" not in response.text

    async def test_download_history_empty_state_points_to_post_processing_history(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_post_processing_only_history_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/downloads/history",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="downloads-history-empty"' in response.text
        assert "0 download-client records" in response.text
        assert "2 post-processing records" in response.text
        assert (
            "Downloads that completed and moved into import/post-processing are shown in "
            "Post-processing History." in response.text
        )
        assert 'href="/post-processing?tab=history"' in response.text
        assert "Wolverine 001 (2026) (Digital).cbz" not in response.text
        assert "Wolverine 002 (2026) (Digital).cbz" not in response.text

    async def test_download_history_supports_search_and_preserves_query_in_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_history_contract_data(sec_db)

        response = await authenticated_client.get(
            "/htmx/downloads/history?search=Batman",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Batman 001 (2024) (Digital).cbz" in response.text
        assert "Batman 002 (2024) (Digital).cbz" in response.text
        assert "Action Comics 050 (2024) (Digital).cbz" not in response.text
        assert 'hx-get="/htmx/downloads/history?page=1&amp;search=Batman"' in response.text

    async def test_download_history_partial_keeps_exact_poll_query_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/downloads/history?status=failed&client=sabnzbd&search=Batman&sort=status&page=2",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        match = re.search(
            r'<div[^>]+id="downloads-history-results"[^>]+hx-get="([^"]+)"',
            response.text,
            re.S,
        )
        assert match is not None

        parsed = urlsplit(unescape(match.group(1)))
        assert parsed.path == "/htmx/downloads/history"
        assert parse_qs(parsed.query) == {
            "page": ["1"],
            "status": ["failed"],
            "client": ["sabnzbd"],
            "search": ["Batman"],
            "sort": ["status"],
        }

    async def test_download_history_supports_sorting_by_table_columns(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_history_contract_data(sec_db)

        default_response = await authenticated_client.get(
            "/htmx/downloads/history",
            headers={"HX-Request": "true"},
        )
        default_html = default_response.text
        assert (
            default_html.index("Action Comics 050 (2024) (Digital).cbz")
            < default_html.index("Batman 001 (2024) (Digital).cbz")
            < default_html.index("Batman 002 (2024) (Digital).cbz")
        )

        size_response = await authenticated_client.get(
            "/htmx/downloads/history?sort=-size",
            headers={"HX-Request": "true"},
        )
        size_html = size_response.text
        assert (
            size_html.index("Batman 002 (2024) (Digital).cbz")
            < size_html.index("Action Comics 050 (2024) (Digital).cbz")
            < size_html.index("Batman 001 (2024) (Digital).cbz")
        )

        issue_response = await authenticated_client.get(
            "/htmx/downloads/history?sort=issue",
            headers={"HX-Request": "true"},
        )
        issue_html = issue_response.text
        assert (
            issue_html.index("Action Comics #50")
            < issue_html.index("Batman #1")
            < issue_html.index("Batman #2")
        )

        status_response = await authenticated_client.get(
            "/htmx/downloads/history?sort=status",
            headers={"HX-Request": "true"},
        )
        status_html = status_response.text
        assert (
            status_html.index("Batman 002 (2024) (Digital).cbz")
            < status_html.index("Action Comics 050 (2024) (Digital).cbz")
            < status_html.index("Batman 001 (2024) (Digital).cbz")
        )

    async def test_download_progress_map_reads_projection_without_polling_clients(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module
        from pullbox.tasks.download_task import ProgressSnapshot

        await _seed_download_queue_contract_data(sec_db)
        async with sec_db() as session:
            queue_item = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.state == DownloadState.DOWNLOADING
                    )
                )
            ).scalar_one()
            await publish_operation_progress(
                session,
                OperationProgressUpdate(
                    operation_type=OperationProgressType.DOWNLOAD,
                    operation_key=str(queue_item.id),
                    revision=4,
                    state=OperationProgressState.RUNNING,
                    phase="downloading",
                    title=queue_item.title,
                    message="Downloading",
                    source_label="Main Usenet",
                    overall=OperationProgressMeasure(
                        current=75_000_000,
                        total=100_000_000,
                        unit="bytes",
                    ),
                    rate=2_000_000,
                    rate_unit="bytes_per_second",
                    eta_seconds=13,
                ),
            )
            await session.commit()

        register = AsyncMock(side_effect=AssertionError("UI must not poll download clients"))
        monkeypatch.setattr(registry_module, "register_download_clients", register)
        async with sec_db() as session:
            queue_item = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.state == DownloadState.DOWNLOADING
                    )
                )
            ).scalar_one()
            progress_map = await ui_routes._load_download_progress_map(
                session,
                [queue_item],
                fallback_progress={
                    queue_item.id: ProgressSnapshot(
                        progress=0.2,
                        speed_bytes=100,
                        eta_seconds=300,
                        size_bytes=100_000_000,
                        updated_at=1.0,
                        client_state="Downloading",
                    )
                },
            )

        snapshot = progress_map[queue_item.id]
        assert snapshot.progress == pytest.approx(0.75)
        assert snapshot.bytes_transferred == 75_000_000
        assert snapshot.speed_bytes == 2_000_000
        assert snapshot.eta_seconds == 13
        assert snapshot.client_state == "Downloading"
        assert snapshot.source_label == "Main Usenet"
        register.assert_not_awaited()

    async def test_download_progress_map_keeps_scheduler_fallback_without_client_poll(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module
        from pullbox.tasks.download_task import ProgressSnapshot

        await _seed_multi_active_download_queue_data(sec_db)

        register = AsyncMock(side_effect=AssertionError("UI must not poll download clients"))
        monkeypatch.setattr(registry_module, "register_download_clients", register)

        async with sec_db() as session:
            queue_items = list(
                (
                    await session.execute(
                        select(DownloadHistory)
                        .where(DownloadHistory.state == DownloadState.DOWNLOADING)
                        .order_by(DownloadHistory.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            fallback = ProgressSnapshot(
                progress=0.44,
                speed_bytes=256_000,
                eta_seconds=180,
                size_bytes=90_000_000,
                updated_at=33.0,
                client_state="Downloading",
            )

            progress_map = await ui_routes._load_download_progress_map(
                session,
                queue_items,
                fallback_progress={queue_items[0].id: fallback},
            )

        assert progress_map[queue_items[0].id] == fallback
        register.assert_not_awaited()

    async def test_download_progress_map_reads_direct_durable_snapshot_without_client_poll(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module

        await _seed_download_queue_contract_data(sec_db)
        async with sec_db() as session:
            issue = (await session.execute(select(Issue).limit(1))).scalar_one()
            attempt = DirectAcquisitionAttempt(
                request_key="downloads-ui:direct:1",
                issue_id=issue.id,
                provider_identity="pullbox.getcomics",
                provider_candidate_id="candidate-1",
                state=DirectAcquisitionState.DOWNLOADING,
                candidate_snapshot={"display_title": "Direct Issue 001"},
                progress_revision=3,
                progress_snapshot={
                    "stage": "downloading",
                    "host_kind": "pixeldrain",
                    "percent": 37,
                    "bytes_per_second": 2048,
                    "eta_seconds": 12,
                    "total_bytes": 1000,
                    "source_slow": True,
                },
            )
            session.add(attempt)
            await session.flush()
            history = DownloadHistory(
                issue_id=issue.id,
                title="Direct Issue 001",
                download_url=f"pullbox-direct://attempt/{attempt.id}",
                download_client=DownloadClientType.DIRECT,
                external_id=f"direct:{attempt.id}",
                state=DownloadState.DOWNLOADING,
            )
            session.add(history)
            await session.commit()

        register = AsyncMock()
        monkeypatch.setattr(registry_module, "register_download_clients", register)
        async with sec_db() as session:
            queue_item = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.download_client == DownloadClientType.DIRECT
                    )
                )
            ).scalar_one()
            progress_map = await ui_routes._load_download_progress_map(
                session,
                [queue_item],
                fallback_progress={},
            )

        snapshot = progress_map[queue_item.id]
        assert snapshot.progress == pytest.approx(0.37)
        assert snapshot.speed_bytes == 2048
        assert snapshot.eta_seconds == 12
        assert snapshot.size_bytes == 1000
        assert snapshot.client_state == "Downloading from PixelDrain"
        assert snapshot.source_label == "GetComics via PixelDrain"
        assert snapshot.source_slow is True
        register.assert_not_awaited()

    async def test_direct_progress_labels_ranked_and_required_resolver_attempts(self) -> None:
        from pullbox.ui import downloads_routes

        assert (
            downloads_routes._direct_progress_label(
                {
                    "stage": "provider_queue",
                    "provider_name": "Library Genesis",
                    "host_kind": "generic_https",
                }
            )
            == "Queued for Library Genesis"
        )

        assert (
            downloads_routes._direct_progress_label(
                {
                    "stage": "resolver",
                    "resolver_name": "FlareSolverr",
                    "resolver_kind": "flaresolverr",
                    "resolver_attempt": 1,
                    "resolver_total": 3,
                    "resolver_scope": "provider:pullbox.getcomics:resolve",
                }
            )
            == "Trying FlareSolverr (resolver 1 of 3)"
        )
        assert (
            downloads_routes._direct_progress_label(
                {
                    "stage": "resolver",
                    "resolver_name": "TRAWL",
                    "resolver_kind": "trawl",
                    "resolver_attempt": 1,
                    "resolver_total": 1,
                    "resolver_scope": "datanodes",
                }
            )
            == "Using TRAWL (required by DataNodes)"
        )

    async def test_airdcpp_progress_map_uses_durable_reconciliation_snapshot(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module

        await _seed_download_queue_contract_data(sec_db)
        async with sec_db() as session:
            issue = (await session.execute(select(Issue).limit(1))).scalar_one()
            history = DownloadHistory(
                issue_id=issue.id,
                title="Air Issue 001.cbz",
                download_url="airdcpp://intent/ui-progress",
                download_client=DownloadClientType.AIRDCPP,
                external_id="airdcpp:12:bundle:91",
                state=DownloadState.DOWNLOADING,
                file_size=100_000_000,
            )
            session.add(history)
            await session.flush()
            session.add(
                AirDcppAcquisition(
                    download_history_id=history.id,
                    request_key="ui-progress",
                    client_config_id=None,
                    client_identity="airdcpp:12",
                    tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                    size_bytes=100_000_000,
                    original_name=history.title,
                    bundle_id=91,
                    client_state="downloading",
                    route_snapshot={
                        "queue": {
                            "version": 1,
                            "downloaded_bytes": 25_000_000,
                            "size_bytes": 100_000_000,
                            "speed_bytes": 1_500_000,
                            "eta_seconds": 50,
                            "status_id": "downloading",
                            "sources_online": 1,
                            "sources_total": 2,
                        }
                    },
                )
            )
            await session.commit()

        register = AsyncMock()
        monkeypatch.setattr(registry_module, "register_download_clients", register)
        async with sec_db() as session:
            queue_item = (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.title == "Air Issue 001.cbz")
                )
            ).scalar_one()
            progress_map = await ui_routes._load_download_progress_map(
                session,
                [queue_item],
                fallback_progress={},
            )

        snapshot = progress_map[queue_item.id]
        assert snapshot.progress == pytest.approx(0.25)
        assert snapshot.speed_bytes == 1_500_000
        assert snapshot.eta_seconds == 50
        assert snapshot.client_state == "Downloading"
        assert snapshot.source_label == "AirDC++"
        register.assert_not_awaited()

    async def test_download_progress_map_preserves_unknown_total_direct_activity(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module

        await _seed_download_queue_contract_data(sec_db)
        async with sec_db() as session:
            issue = (await session.execute(select(Issue).limit(1))).scalar_one()
            attempt = DirectAcquisitionAttempt(
                request_key="downloads-ui:direct:unknown-total",
                issue_id=issue.id,
                provider_identity="pullbox.getcomics",
                provider_candidate_id="candidate-terabox",
                state=DirectAcquisitionState.DOWNLOADING,
                candidate_snapshot={"display_title": "Unknown Total Issue"},
                progress_revision=8,
                progress_snapshot={
                    "stage": "downloading",
                    "host_kind": "terabox",
                    "percent": None,
                    "bytes_transferred": 43_492_898,
                    "bytes_per_second": 1_048_576,
                    "eta_seconds": None,
                    "total_bytes": None,
                },
            )
            session.add(attempt)
            await session.flush()
            history = DownloadHistory(
                issue_id=issue.id,
                title="Unknown Total Issue",
                download_url=f"pullbox-direct://attempt/{attempt.id}",
                download_client=DownloadClientType.DIRECT,
                external_id=f"direct:{attempt.id}",
                state=DownloadState.DOWNLOADING,
            )
            session.add(history)
            await session.commit()

        register = AsyncMock()
        monkeypatch.setattr(registry_module, "register_download_clients", register)
        async with sec_db() as session:
            queue_item = (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.title == "Unknown Total Issue")
                )
            ).scalar_one()
            progress_map = await ui_routes._load_download_progress_map(
                session,
                [queue_item],
                fallback_progress={},
            )

        snapshot = progress_map[queue_item.id]
        assert snapshot.progress == 0.0
        assert snapshot.bytes_transferred == 43_492_898
        assert snapshot.is_indeterminate is True
        assert snapshot.speed_bytes == 1_048_576
        assert snapshot.eta_seconds is None
        assert snapshot.size_bytes is None
        assert snapshot.client_state == "Downloading from TeraBox"
        register.assert_not_awaited()

    async def test_download_queue_context_builds_active_and_waiting_row_views(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_download_queue_contract_data(sec_db)

        async def _fake_progress_map(session, queue_items, *, fallback_progress):  # type: ignore[no-untyped-def]
            del session
            del fallback_progress
            return {
                queue_items[0].id: SimpleNamespace(
                    progress=0.53,
                    speed_bytes=1_200_000,
                    eta_seconds=65,
                    size_bytes=104_857_600,
                    updated_at=55.0,
                    client_state="Downloading",
                )
            }

        async def _fake_queue_names(session, queue_items):  # type: ignore[no-untyped-def]
            del session
            return {queue_items[0].id: "Batman 002 Renamed.cbz"}

        monkeypatch.setattr(ui_routes, "_load_download_progress_map", _fake_progress_map)
        monkeypatch.setattr(ui_routes, "_build_queue_names", _fake_queue_names)

        async with sec_db() as session:
            ctx = await ui_routes._load_download_queue_context(session)

        assert ctx["active_count"] == 1
        assert ctx["waiting_count"] == 1
        assert ctx["combined_speed_bytes"] == 1_200_000
        active_rows = ctx["active_rows"]
        waiting_rows = ctx["waiting_rows"]
        assert len(active_rows) == 1
        assert len(waiting_rows) == 1
        assert active_rows[0].display_title == "Batman 002 Renamed.cbz"
        assert active_rows[0].primary_phase == "Downloading"
        assert waiting_rows[0].primary_phase == "Queued"

    async def test_download_progress_map_handles_missing_client_and_unmatched_status(
        self,
        sec_db,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import pullbox.composition.providers as registry_module
        from pullbox.tasks.download_task import ProgressSnapshot

        await _seed_multi_active_download_queue_data(sec_db)

        fake_client = SimpleNamespace(
            client_type=DownloadClientType.QBITTORRENT.value,
            get_queue=AsyncMock(return_value=[]),
        )

        async def _fake_register(session, registry):  # type: ignore[no-untyped-def]
            del session
            registry.register_download_client(1, fake_client)
            return []

        monkeypatch.setattr(registry_module, "register_download_clients", _fake_register)

        async with sec_db() as session:
            queue_items = list(
                (
                    await session.execute(
                        select(DownloadHistory)
                        .where(DownloadHistory.state == DownloadState.DOWNLOADING)
                        .order_by(DownloadHistory.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            fallback = ProgressSnapshot(
                progress=0.21,
                speed_bytes=111_000,
                eta_seconds=210,
                size_bytes=90_000_000,
                updated_at=88.0,
                client_state="Downloading",
            )

            progress_map = await ui_routes._load_download_progress_map(
                session,
                queue_items,
                fallback_progress={queue_items[0].id: fallback},
            )

        assert progress_map[queue_items[0].id] == fallback


class TestDownloadQueueRowViewHelpers:
    """Direct helper coverage for stable queue lifecycle mapping."""

    def test_paused_queue_row_uses_warning_phase_and_preserves_progress(self) -> None:
        download = DownloadHistory(
            title="Paused Issue.cbz",
            state=DownloadState.PAUSED,
            download_client=DownloadClientType.SABNZBD,
            download_url="https://example.com/paused.nzb",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(progress=0.42, speed_bytes=None, eta_seconds=None, client_state=None),
            None,
        )

        assert row.primary_phase == "Paused"
        assert row.status_pill == "pill-warning"
        assert row.progress_tone == "is-amber"
        assert row.progress_label == "42%"

    def test_retry_pending_queue_row_uses_warning_phase_and_preserves_progress(self) -> None:
        download = DownloadHistory(
            title="Retry Issue.cbz",
            state=DownloadState.RETRY_PENDING,
            download_client=DownloadClientType.SABNZBD,
            download_url="https://example.com/retry.nzb",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(progress=0.18, speed_bytes=None, eta_seconds=None, client_state=None),
            None,
        )

        assert row.primary_phase == "Retry pending"
        assert row.status_pill == "pill-warning"
        assert row.progress_tone == "is-amber"
        assert row.progress_label == "18%"

    def test_direct_download_row_identifies_the_active_fallback_host(self) -> None:
        download = DownloadHistory(
            title="Direct Issue.cbz",
            state=DownloadState.DOWNLOADING,
            download_client=DownloadClientType.DIRECT,
            download_url="pullbox-direct://attempt/7",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(
                progress=0.37,
                speed_bytes=2048,
                eta_seconds=12,
                client_state="Downloading from PixelDrain",
                source_label="GetComics via PixelDrain",
            ),
            None,
        )

        assert row.primary_phase == "Downloading from PixelDrain"
        assert row.client_label == "GetComics via PixelDrain"
        assert row.progress_label == "37%"

    def test_direct_download_row_reports_sustained_slow_source(self) -> None:
        download = DownloadHistory(
            title="Slow Direct Issue.cbz",
            state=DownloadState.DOWNLOADING,
            download_client=DownloadClientType.DIRECT,
            download_url="pullbox-direct://attempt/8",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(
                progress=0.42,
                speed_bytes=34 * 1024,
                eta_seconds=600,
                client_state="Downloading from HTTPS",
                source_label="Anna's Archive via HTTPS",
                source_slow=True,
            ),
            None,
        )

        assert row.primary_phase == "Source responding slowly"
        assert row.status_pill == "pill-warning"
        assert row.progress_tone == "is-amber"
        assert row.status_detail == (
            "The source is still transferring data below 500 kbps. Pullbox will keep downloading."
        )
        assert row.speed_bytes == 34 * 1024

    def test_direct_download_row_describes_unknown_total_without_fake_percent(self) -> None:
        download = DownloadHistory(
            title="Unknown Total Issue.cbz",
            state=DownloadState.DOWNLOADING,
            download_client=DownloadClientType.DIRECT,
            download_url="pullbox-direct://attempt/9",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(
                progress=0.0,
                speed_bytes=1_048_576,
                eta_seconds=None,
                size_bytes=None,
                bytes_transferred=43_492_898,
                is_indeterminate=True,
                client_state="Downloading from TeraBox",
                source_label="GetComics via TeraBox",
            ),
            None,
        )

        assert row.primary_phase == "Downloading from TeraBox"
        assert row.progress_indeterminate is True
        assert row.progress_pct == 0.0
        assert row.progress_label == "41.5 MB received"
        assert row.speed_bytes == 1_048_576
        assert row.eta_text is None

    def test_direct_retry_pending_row_preserves_provider_host_and_failure_detail(self) -> None:
        download = DownloadHistory(
            title="Direct Retry.cbz",
            state=DownloadState.RETRY_PENDING,
            download_client=DownloadClientType.DIRECT,
            download_url="pullbox-direct://attempt/8",
            retry_count=1,
            max_retries=3,
            error_message="The artifact transfer stopped making progress.",
        )

        row = ui_routes._build_download_queue_row_view(
            download,
            SimpleNamespace(
                progress=0.2,
                speed_bytes=None,
                eta_seconds=None,
                client_state="Retry pending",
                source_label="Anna's Archive via HTTPS",
            ),
            None,
        )

        assert row.primary_phase == "Retry pending"
        assert row.client_label == "Anna's Archive via HTTPS"
        assert row.status_detail == ("The artifact transfer stopped making progress. Retry 1 of 3.")
