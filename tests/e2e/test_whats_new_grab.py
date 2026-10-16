"""Inline Grab uses real UI/API/history with only external search/client I/O faked."""
# ruff: noqa: F811 - reuse the owning release fixtures.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from playwright.sync_api import expect
from sqlalchemy import delete

from pullbox.api.v1 import issues as issue_api
from pullbox.composition import services
from pullbox.core.events import EventBus
from pullbox.database import get_session_factory
from pullbox.models import DownloadHistory
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.schemas.search import MatchDetails, RejectedResultItem, SearchResultItem
from pullbox.services.download_service import DownloadService
from pullbox.services.search_targets import load_issue_search_target
from pullbox.services.whats_new_data_client import PullboxDataClientError, WhatsNewDataClient
from tests.api.test_issue_grab import _mock_nzb_client, _mock_registry
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_whats_new_find_add import release_page  # noqa: F401
from tests.e2e.test_whats_new_issue_state import issue_state_page  # noqa: F401

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def isolate_release_feed():
    # Isolate external feed I/O before the server starts its background refresh.
    with (
        patch.object(
            WhatsNewDataClient,
            "get_current_week",
            AsyncMock(side_effect=PullboxDataClientError("Offline release fixture")),
        ),
    ):
        yield


@pytest.fixture
def grab_page(issue_state_page, monkeypatch):
    page, issue_ids = issue_state_page
    selected_issue = issue_ids[0]

    async def prepare():
        async with get_session_factory().begin() as session:
            indexer = IndexerConfig(
                name="Inline fixture",
                indexer_type=IndexerType.NEWZNAB,
                url="https://example.test",
                api_key="test",
            )
            client = DownloadClientConfig(
                name="Inline fixture",
                client_type=DownloadClientType.SABNZBD,
                url="https://example.test",
            )
            session.add_all([indexer, client])
            await session.flush()
            return indexer.id, client.id

    indexer_id, client_id = _run_async_blocking(prepare())
    calls = []
    mode = {"fail_search": False, "rejected": False, "slow_search": False}

    async def search(session, issue_id, **_kwargs):
        calls.append(issue_id)
        if mode["fail_search"]:
            raise HTTPException(503, "Test source temporarily unavailable")
        if mode["slow_search"]:
            await asyncio.sleep(11)
        target = await load_issue_search_target(session, issue_id)
        return issue_api._IssueSearchBundle(
            target=target,
            issue=issue_api._build_issue_context(target),
            runtime=SimpleNamespace(),
            outcome=None,
            search_time_ms=10,
            matched_items=[
                SearchResultItem(
                    title=f"{target.series_title} 001 (2026).cbz",
                    indexer_name="Inline fixture",
                    indexer_id=indexer_id,
                    download_url="https://example.test/comic",
                    confidence="high",
                    quality_score=80,
                    auto_grabbable=True,
                    is_torrent=False,
                    match_details=MatchDetails(
                        parsed_series=target.series_title,
                        parsed_issue=1,
                        parsed_year=2026,
                        series_similarity=1,
                        match_type="exact",
                    ),
                )
            ],
            rejected_items=[
                RejectedResultItem(
                    title="Another comic 001.cbz",
                    indexer_name="Inline fixture",
                    indexer_id=indexer_id,
                    download_url="https://example.test/other",
                    is_torrent=False,
                    rejection_reason="Series name disagrees",
                )
            ]
            if mode["rejected"]
            else [],
        )

    client = _mock_nzb_client()
    registry = _mock_registry(nzb=client)
    monkeypatch.setattr(issue_api, "_run_issue_search", search)
    monkeypatch.setattr(
        services,
        "build_domain_download_service",
        AsyncMock(return_value=(DownloadService(registry, EventBus()), {})),
    )
    yield page, selected_issue, calls, client, mode

    async def cleanup():
        async with get_session_factory().begin() as session:
            await session.execute(
                delete(DownloadHistory).where(DownloadHistory.issue_id == selected_issue)
            )
            await session.execute(delete(IndexerConfig).where(IndexerConfig.id == indexer_id))
            await session.execute(
                delete(DownloadClientConfig).where(DownloadClientConfig.id == client_id)
            )

    _run_async_blocking(cleanup())


def open_picker(page, seeded_server):
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    with page.expect_response(lambda response: "/search-results" in response.url) as search:
        row.get_by_test_id("whats-new-grab").click()
    assert search.value.status == 200, search.value.text()
    dialog = page.get_by_role("dialog", name="ISSUE SEARCH")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("issue-search-results-matched-row")).to_have_count(1)
    return row, dialog


def test_explicit_grab_keeps_table_and_reads_actual_queue(grab_page, seeded_server):
    page, issue_id, calls, client, _mode = grab_page
    row, dialog = open_picker(page, seeded_server)
    table = page.get_by_test_id("whats-new-current-release-table")
    table.evaluate("el => el.dataset.checkpoint = 'unchanged'")
    assert calls == [issue_id]
    client.add_nzb.assert_not_awaited()
    dialog.get_by_role("button", name="Grab", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Queued")
    expect(row.get_by_test_id("whats-new-grab")).to_be_disabled()
    expect(row.get_by_test_id("whats-new-local-issue")).to_be_focused()
    expect(table).to_have_attribute("data-checkpoint", "unchanged")
    client.add_nzb.assert_awaited_once()
    page.reload()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Queued")
    expect(row.get_by_test_id("whats-new-grab")).to_have_count(0)


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("system", 320)])
def test_picker_cancel_focus_reflow_and_accessibility(
    grab_page, seeded_server, theme, width, browser_name
):
    page, _issue_id, _calls, client, _mode = grab_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    page.evaluate("theme => applyTheme(theme)", theme)
    expected_theme = (
        page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'")
        if theme == "system"
        else theme
    )
    expect(page.locator("html")).to_have_attribute("data-theme", expected_theme)
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    trigger = row.get_by_test_id("whats-new-grab")
    trigger.click()
    dialog = page.get_by_role("dialog", name="ISSUE SEARCH")
    expect(dialog.get_by_test_id("issue-search-results-matched-row")).to_be_visible()
    expect(dialog).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_test_id("issue-search-modal-footer-close")).to_be_focused()
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert_no_axe_violations(page, name=f"inline-grab-{theme}-{width}")
    dialog.screenshot(
        path=f"output/playwright/inline-grab-{browser_name}-{theme}-{width}.png",
        animations="disabled",
    )
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()
    client.add_nzb.assert_not_awaited()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Missing")


def test_search_failure_is_not_an_endless_spinner(grab_page, seeded_server):
    page, _issue_id, _calls, client, mode = grab_page
    mode["fail_search"] = True
    page.goto(f"{seeded_server}/whats-new")
    page.get_by_test_id("whats-new-grab").first.click()
    dialog = page.get_by_role("dialog", name="ISSUE SEARCH")
    expect(dialog.get_by_role("alert")).to_be_visible()
    expect(dialog.get_by_text("Searching indexers", exact=True)).not_to_be_visible()
    client.add_nzb.assert_not_awaited()


def test_slow_search_does_not_leave_a_false_error(grab_page, seeded_server):
    page, issue_id, calls, client, mode = grab_page
    mode["slow_search"] = True
    page.goto(f"{seeded_server}/whats-new")
    with page.expect_response(
        lambda response: "/search-results" in response.url, timeout=20000
    ) as search:
        page.get_by_test_id("whats-new-grab").first.click()
    assert search.value.status == 200
    dialog = page.get_by_role("dialog", name="ISSUE SEARCH")
    expect(dialog.get_by_test_id("issue-search-results-matched-row")).to_be_visible()
    expect(dialog.get_by_role("alert")).not_to_be_visible()
    expect(dialog.get_by_text("Searching indexers", exact=True)).not_to_be_visible()
    assert calls == [issue_id]
    client.add_nzb.assert_not_awaited()
    dialog.get_by_test_id("issue-search-modal-footer-close").click()
    expect(dialog).not_to_be_visible()


def test_failed_grab_stays_open_and_allows_explicit_retry(grab_page, seeded_server):
    page, _issue_id, _calls, client, _mode = grab_page
    row, dialog = open_picker(page, seeded_server)
    page.route(
        "**/api/v1/whats-new/grab",
        lambda route: route.fulfill(status=503, json={"detail": "Download client unavailable"}),
    )
    dialog.get_by_role("button", name="Grab", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("Download client unavailable")
    expect(dialog.get_by_role("button", name="Grab", exact=True)).to_be_enabled()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Missing")
    client.add_nzb.assert_not_awaited()
    page.unroute("**/api/v1/whats-new/grab")
    dialog.get_by_role("button", name="Grab", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Queued")
    client.add_nzb.assert_awaited_once()


def test_rejected_result_requires_confirmation_and_cancel_sends_nothing(grab_page, seeded_server):
    page, _issue_id, _calls, client, mode = grab_page
    mode["rejected"] = True
    _row, dialog = open_picker(page, seeded_server)
    dialog.get_by_role("button", name="Grab anyway", exact=True).click()
    confirm = page.get_by_role("dialog", name="Grab Rejected Result", exact=True)
    expect(confirm.get_by_role("heading", name="Grab Rejected Result")).to_be_visible()
    client.add_nzb.assert_not_awaited()
    confirm.get_by_role("button", name="Cancel", exact=True).click()
    expect(confirm).not_to_be_visible()
    expect(dialog).to_be_visible()
    dialog.get_by_test_id("issue-search-modal-footer-close").click()
    client.add_nzb.assert_not_awaited()
