"""Route-contract tests for the unified Import Follow-up tab."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-orphaned-shell-ui")


@pytest.mark.asyncio
class TestImportFollowUpTabRouteContracts:
    """Verify Follow-up is mounted under the unified Import workspace."""

    async def test_legacy_unmatched_route_redirects_to_unified_import_page(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import/orphaned")

        assert response.status_code == 307
        assert response.headers["location"] == "/import?tab=follow-up&view=all"

    async def test_import_orphaned_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import?tab=unmatched")

        assert response.status_code == 200
        assert 'data-testid="import-page"' in response.text
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-header-gauges-spacer"' in response.text
        assert 'data-testid="import-header-gauges"' not in response.text
        assert 'data-testid="import-tabs"' in response.text
        assert 'data-testid="import-footer-dock"' in response.text
        assert 'data-testid="import-follow-up-page"' in response.text
        assert 'data-testid="import-orphaned-body"' in response.text
        assert 'data-testid="import-orphaned-results"' in response.text
        assert 'data-testid="import-follow-up-job-list"' in response.text
        assert 'data-testid="import-follow-up-view"' in response.text
        assert 'data-testid="import-orphaned-modal-host"' in response.text

    async def test_import_orphaned_hx_request_returns_results_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/import?tab=unmatched&view=dismissed",
            headers={"HX-Request": "true", "HX-Target": "import-orphaned-results"},
        )

        assert response.status_code == 200
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-tabs"' in response.text
        assert 'data-testid="import-orphaned-results"' in response.text
        assert 'data-testid="import-orphaned-tabs"' in response.text
        assert 'data-testid="import-orphaned-view"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'data-testid="import-content"' not in response.text
        assert 'data-testid="import-follow-up-page"' not in response.text
