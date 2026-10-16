"""The existing search modal is cache-bound when opened from What's New."""
# ruff: noqa: F811 - owning fixtures are intentionally imported.

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.api.v1 import issues
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.models.issue import Issue, IssueStatus
from tests.api.test_whats_new_grab_api import selected_release  # noqa: F401
from tests.ui.test_whats_new_issue_state import actions_enabled  # noqa: F401

pytest_plugins = ["conftest_security"]


async def test_confirmed_missing_release_opens_shared_picker(
    authenticated_client, sec_db, selected_release
):
    async with sec_db.begin() as session:
        session.add_all(
            [
                IndexerConfig(
                    name="Search",
                    indexer_type=IndexerType.NEWZNAB,
                    url="https://example.test",
                    api_key="test",
                ),
                DownloadClientConfig(
                    name="Client",
                    client_type=DownloadClientType.SABNZBD,
                    url="https://example.test",
                ),
            ]
        )
    response = await authenticated_client.get("/whats-new")
    assert 'data-testid="whats-new-grab"' in response.text, "missing release has no inline Grab"
    assert 'data-testid="issue-search-modal"' in response.text
    button = re.search(
        r'<button\b[^>]*data-testid="whats-new-grab".*?</button>', response.text, re.S
    )
    assert button and "open-search" in button[0]
    assert selected_release.fingerprint in button[0]


async def test_stale_search_never_contacts_sources(
    authenticated_client, selected_release, monkeypatch
):
    search = AsyncMock()
    monkeypatch.setattr(issues, "_run_issue_search", search)
    selected_release.fingerprint = "0" * 64
    response = await authenticated_client.get(
        f"/htmx/issues/{selected_release.issue_id}/search-results",
        params={"release_selection": selected_release.model_dump_json()},
    )
    assert response.status_code == 409, response.text
    search.assert_not_awaited()


async def test_issue_changed_during_source_search_is_not_rendered(
    authenticated_client, selected_release, monkeypatch
):
    async def change_issue(session, issue_id, **_kwargs):
        issue = await session.get(Issue, issue_id)
        issue.status = IssueStatus.SKIPPED
        await session.flush()
        return SimpleNamespace()

    search = AsyncMock(side_effect=change_issue)
    monkeypatch.setattr(issues, "_run_issue_search", search)
    response = await authenticated_client.get(
        f"/htmx/issues/{selected_release.issue_id}/search-results",
        params={"release_selection": selected_release.model_dump_json()},
    )
    assert response.status_code == 409, response.text
    assert "skipped" in response.text
    search.assert_awaited_once()


@pytest.mark.parametrize("route", ["dc-search-status", "dc-search-results"])
async def test_direct_connect_entrypoints_revalidate_same_context(
    authenticated_client, selected_release, route
):
    selected_release.fingerprint = "0" * 64
    response = await authenticated_client.get(
        f"/htmx/issues/{selected_release.issue_id}/{route}",
        params={"release_selection": selected_release.model_dump_json()},
    )
    assert response.status_code == 409, response.text
