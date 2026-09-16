"""Characterization coverage for legacy import redirect routes."""

from unittest.mock import MagicMock

import pytest

from pullbox.ui.import_redirect_routes import import_history_redirect, import_orphaned_redirect


@pytest.mark.asyncio
async def test_import_history_redirect_targets_unified_history_tab() -> None:
    response = await import_history_redirect(MagicMock(), MagicMock(), MagicMock())

    assert response.status_code == 307
    assert response.headers["location"] == "/import?tab=history"


@pytest.mark.asyncio
async def test_import_orphaned_redirect_preserves_legacy_filters() -> None:
    response = await import_orphaned_redirect(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        tab="dismissed",
        page=3,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/import?tab=follow-up&view=dismissed&page=3"
