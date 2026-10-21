"""Keep navigation available during search and recover CSS without losing the page."""

import asyncio
import threading

import pytest
from fastapi import HTTPException
from playwright.sync_api import expect

from tests.e2e.pages.app_shell import AppShellPage

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("failure", ["network", "invalid_css"])
def test_stylesheet_failure_is_bounded_preserves_page_and_retry_recovers(
    authed_page, seeded_server, failure
):
    page = authed_page
    requests = []
    activity = []
    recovered = False

    def stylesheet(route):
        requests.append(route.request.url)
        if recovered:
            route.continue_()
        elif failure == "network":
            route.abort()
        else:
            route.fulfill(content_type="text/css", body="/* Incomplete stylesheet */")

    page.route("**/static/css/tailwind.css*", stylesheet)
    page.on(
        "request",
        lambda request: activity.append(request.url) if "/api/v1/activity" in request.url else None,
    )
    page.goto(seeded_server + "/series", wait_until="domcontentloaded")
    page.evaluate("window.originalMain = document.getElementById('main-area')")
    expect(page.get_by_role("heading", name="Pullbox could not load its interface")).to_be_visible(
        timeout=2000
    )
    assert len(requests) == 3
    assert activity == []
    assert page.evaluate("originalMain.isConnected")
    recovered = True
    page.get_by_role("button", name="Retry loading Pullbox").click()
    expect(page.locator("html")).to_have_attribute("data-pullbox-stylesheet-ready", "true")
    expect(page.locator("body")).not_to_have_attribute("data-shell-pending", "")
    expect(page.locator("#pullbox-stylesheet-recovery")).to_have_count(0)
    assert page.evaluate("originalMain === document.getElementById('main-area')")
    assert len(requests) == 4
    assert page.evaluate("performance.getEntriesByType('navigation')[0].type") == "navigate"


def test_slow_stylesheet_stays_inflight_and_late_success_restores_original_page(
    authed_page, seeded_server
):
    page = authed_page
    requests = []

    def stylesheet(route):
        requests.append(route)
        if len(requests) == 1:
            route.abort()

    page.route("**/static/css/tailwind.css*", stylesheet)
    page.goto(seeded_server + "/series", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.querySelector('#pullbox-app-stylesheet').href.includes('retry')"
    )
    page.evaluate("window.originalMain = document.getElementById('main-area')")
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now() + 100"))
    page.clock.run_for(12000)
    expect(page.get_by_role("heading", name="Pullbox is still loading")).to_be_visible()
    assert len(requests) == 2
    assert page.evaluate("originalMain.isConnected")
    requests[1].continue_()
    page.wait_for_function("document.documentElement.dataset.pullboxStylesheetReady === 'true'")
    page.clock.resume()
    expect(page.locator("#pullbox-stylesheet-recovery")).to_have_count(0)
    expect(page.locator("body")).not_to_have_attribute("data-shell-pending", "")
    assert page.evaluate("originalMain === document.getElementById('main-area')")
    assert len(requests) == 2


def test_eight_tabs_load_while_issue_search_is_waiting(authed_page, seeded_server, monkeypatch):
    from pullbox.api.v1 import issues

    started = threading.Event()
    release = threading.Event()

    async def waiting_search(*args, **kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise HTTPException(503, "Test search completed without contacting providers")

    monkeypatch.setattr(issues, "_run_issue_search", waiting_search)
    context = authed_page.context
    context.add_init_script("""(() => {
        window.openedStreams = [];
        const Source = window.EventSource;
        window.EventSource = function(url, options) {
            openedStreams.push(String(url));
            return new Source(url, options);
        };
    })();""")
    AppShellPage(authed_page, seeded_server).goto("/issues/1")
    authed_page.evaluate("""() => {
        window.pendingSearch = fetch('/htmx/issues/1/search-results')
            .then(response => { window.searchStatus = response.status; });
    }""")
    assert started.wait(3)
    tabs = []
    try:
        for _ in range(8):
            tab = context.new_page()
            tabs.append(tab)
            tab.goto(seeded_server + "/issues/1", wait_until="domcontentloaded", timeout=5000)
            expect(tab.locator("body")).not_to_have_attribute(
                "data-shell-pending", "", timeout=3000
            )
            assert tab.evaluate("!openedStreams.includes('/api/v1/activity/stream')")
        assert not release.is_set()
        assert authed_page.evaluate("window.searchStatus === undefined")
        assert authed_page.evaluate("!openedStreams.includes('/api/v1/activity/stream')")
    finally:
        release.set()
        for tab in tabs:
            tab.close()
    authed_page.wait_for_function("window.searchStatus === 503")
