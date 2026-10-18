"""Exercise the real shell with deterministic activity transport and time."""

import pytest
from playwright.sync_api import expect

from tests.e2e.pages.app_shell import AppShellPage

pytestmark = pytest.mark.e2e


@pytest.fixture
def activity_page(authed_page, seeded_server):
    page = authed_page
    page.add_init_script(r"""
        window.activityRequests = [];
        const originalFetch = window.fetch;
        window.fetch = function(url, options) {
            if (String(url).match(/^\/api\/v1\/activity(?:\?|$)/)) {
                return new Promise((resolve, reject) => {
                    const request = {url, resolve: payload => resolve({ok: true,
                        json: () => Promise.resolve(payload)
                    }), reject};
                    window.activityRequests.push(request);
                    if (options.signal) options.signal.addEventListener('abort', () => {
                        request.aborted = true;
                        reject(new DOMException('Aborted', 'AbortError'));
                    });
                });
            }
            return originalFetch.apply(this, arguments);
        };
        const OriginalSource = window.EventSource;
        window.EventSource = function(url) {
            if (url !== '/api/v1/activity/stream') return new OriginalSource(url);
            const source = {__v_skip: true, listeners: {}, addEventListener(name, handler) {
                this.listeners[name] = handler;
            }, close() { this.closed = true; }};
            window.activityStream = source;
            return source;
        };
    """)
    AppShellPage(page, seeded_server).goto("/series")
    page.wait_for_function("activityRequests.length === 1")
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now() + 1000"))
    yield page


def test_activity_never_reserves_a_permanent_browser_connection(activity_page):
    page = activity_page
    assert page.evaluate("!window.activityStream")
    resolve(page, 0, payload())
    page.clock.run_for(3001)
    assert page.evaluate("activityRequests.length") == 2


def test_hidden_tab_releases_request_and_refreshes_on_return(activity_page):
    page = activity_page
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.clock.run_for(1)
    assert page.evaluate("activityRequests[0].aborted")
    page.clock.run_for(60000)
    assert page.evaluate("activityRequests.length") == 1
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_function("activityRequests.length === 2")


def payload(*, state="running", count=1, total=None, ids=None, expiry=None):
    return {
        "operations": [
            {
                "id": i,
                "title": f"Download {i}",
                "state": state,
                "operation_type": "download",
                "overall": {"indeterminate": True},
                "item": None,
            }
            for i in (ids if ids is not None else [1])
        ],
        "active_count": count,
        "spinner_count": count,
        "attention_count": 0,
        "total_count": total if total is not None else count,
        "refresh_after_ms": expiry,
    }


def resolve(page, index, data):
    page.evaluate("([index, data]) => activityRequests[index].resolve(data)", [index, data])
    page.clock.run_for(1)
    assert page.evaluate(
        "!Alpine.$data(document.querySelector('[x-data=\"appShell()\"]')).activityRefreshing"
    )


def test_poll_does_not_overlap_an_inflight_refresh(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.clock.run_for(3001)
    page.clock.run_for(10000)
    assert page.evaluate("activityRequests.length") == 2


def test_inflight_invalidation_is_replayed(activity_page):
    page = activity_page
    page.evaluate("Alpine.$data(document.body).refreshActivity()")
    page.evaluate("activityRequests[0].resolve({operations: [], active_count: 0})")
    page.wait_for_function("activityRequests.length === 2", timeout=2000)


def test_completion_expires_before_next_regular_poll(activity_page):
    page = activity_page
    resolve(page, 0, payload(state="completed", count=0, total=1, expiry=1000))
    page.clock.run_for(1001)
    assert page.evaluate("activityRequests.length") == 2
    resolve(page, 1, payload(count=0, ids=[]))
    assert page.evaluate("Alpine.$data(document.body).activityOperations.length") == 0


@pytest.mark.parametrize("theme", ["light", "dark", "system"])
def test_open_refreshes_and_pagination_exposes_all_jobs(activity_page, theme):
    page = activity_page
    page.evaluate("theme => applyTheme(theme)", theme)
    page.set_viewport_size({"width": 390, "height": 568})
    resolve(page, 0, payload(count=27, ids=list(range(1, 21))))
    page.get_by_role("button", name="Background activity", exact=True).click()
    page.wait_for_function("activityRequests.length === 2", timeout=2000)
    resolve(page, 1, payload(count=27, ids=list(range(1, 21))))
    panel = page.get_by_test_id("header-activity-popover")
    page.clock.run_for(200)
    expect(panel).to_be_in_viewport(ratio=1)
    expect(panel).to_contain_text("1-20 of 27")
    expect(panel.get_by_role("button", name="Next activity page")).to_be_in_viewport(ratio=1)
    panel.get_by_role("button", name="Next activity page").click()
    page.wait_for_function("activityRequests.length === 3")
    assert "offset=20" in page.evaluate("activityRequests[2].url")
    resolve(page, 2, payload(count=27, ids=list(range(21, 28))))
    expect(panel).to_contain_text("21-27 of 27")
    expect(panel).to_contain_text("27 active")
    expect(panel.get_by_test_id("header-activity-operation")).to_have_count(7)
    page.screenshot(path=f"/private/tmp/pullbox-bug3-activity-{theme}.png")


def test_failed_fetch_retries_without_blanking(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.clock.run_for(3001)
    page.evaluate("activityRequests[1].reject(new Error('offline'))")
    page.clock.run_for(3001)
    assert page.evaluate("activityRequests.length") == 3
    assert page.evaluate("Alpine.$data(document.body).activityOperations[0].id") == 1
    resolve(page, 2, payload(count=2, ids=[1, 2]))
    assert page.evaluate("Alpine.$data(document.body).activityBadgeCount()") == 2


def test_slow_fetch_times_out_and_destroy_cancels_refreshes(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.clock.run_for(3001)
    page.clock.run_for(15001)
    assert page.evaluate("activityRequests[1].aborted")
    page.clock.run_for(3001)
    assert page.evaluate("activityRequests.length") == 3
    page.evaluate("Alpine.$data(document.body).destroy()")
    assert page.evaluate("activityRequests[2].aborted")
    page.clock.run_for(20000)
    assert page.evaluate("activityRequests.length") == 3


def test_pagehide_stops_polling_and_bfcache_return_refreshes(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.clock.run_for(10000)
    assert page.evaluate("activityRequests.length") == 1
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.wait_for_function("activityRequests.length === 2")
    resolve(page, 1, payload(count=2, ids=[1, 2]))
    assert page.evaluate("Alpine.$data(document.body).activityBadgeCount()") == 2


def test_shrinking_page_clamps_without_blank_flash_or_losing_focus(activity_page):
    page = activity_page
    resolve(page, 0, payload(count=21, ids=list(range(1, 21))))
    button = page.get_by_role("button", name="Background activity", exact=True)
    button.click()
    resolve(page, 1, payload(count=21, ids=list(range(1, 21))))
    page.clock.run_for(200)
    panel = page.get_by_test_id("header-activity-popover")
    panel.get_by_role("button", name="Next activity page").click()
    resolve(page, 2, payload(count=21, ids=[21]))
    button.focus()
    page.evaluate(
        "window.savedRow = document.querySelector('[data-testid=header-activity-operation]')"
    )
    page.clock.run_for(3001)
    page.evaluate("activityRequests[3].resolve({operations: [], total_count: 1, active_count: 1})")
    page.clock.run_for(1)
    assert page.evaluate("activityRequests.length") == 5
    assert page.evaluate("savedRow.isConnected")
    expect(panel).to_contain_text("Download 21")
    resolve(page, 4, payload(count=1, ids=[21]))
    assert page.evaluate(
        "savedRow === document.querySelector('[data-testid=header-activity-operation]')"
    )
    expect(button).to_be_focused()
    expect(panel).to_be_visible()
    expect(panel.get_by_text("No background work", exact=True)).to_be_hidden()


def test_paging_does_not_flash_disabled_during_background_refresh(activity_page):
    page = activity_page
    resolve(page, 0, payload(count=27, ids=list(range(1, 21))))
    page.get_by_role("button", name="Background activity", exact=True).click()
    resolve(page, 1, payload(count=27, ids=list(range(1, 21))))
    page.clock.run_for(200)
    page.clock.run_for(3001)
    next_button = page.get_by_role("button", name="Next activity page")
    expect(next_button).to_be_enabled()
    next_button.click()
    page.evaluate(
        "data => activityRequests[2].resolve(data)", payload(count=27, ids=list(range(1, 21)))
    )
    page.clock.run_for(1)
    assert page.evaluate("activityRequests.length") == 4
    assert "offset=20" in page.evaluate("activityRequests[3].url")
    resolve(page, 3, payload(count=27, ids=list(range(21, 28))))
    expect(page.get_by_test_id("header-activity-operation")).to_have_count(7)


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_recovery_notification_can_be_dismissed_without_cancel_or_spinner(activity_page, theme):
    page = activity_page
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    data = payload(state="failed", count=0, total=1)
    data["attention_count"] = 1
    data["operations"][0].update(
        attention_required=True,
        acknowledged_at=None,
        message="AirDC++ could not confirm this request.",
        detail_url="/intervention?tab=recovery&protocol=dc",
    )
    resolve(page, 0, data)
    page.get_by_role("button", name="Background activity", exact=True).click()
    resolve(page, 1, data)
    page.clock.run_for(200)
    panel = page.get_by_test_id("header-activity-popover")
    expect(panel.get_by_test_id("header-activity-overall-progress")).to_be_hidden()
    expect(panel.get_by_test_id("header-activity-view-details")).to_have_attribute(
        "href", "/intervention?tab=recovery&protocol=dc"
    )
    calls = []

    def dismiss(route):
        calls.append((route.request.method, route.request.url))
        route.fulfill(json={"acknowledged": True})

    page.route("**/api/v1/activity/1/acknowledge", dismiss)
    panel.get_by_role("button", name="Dismiss notification", exact=True).click()
    page.wait_for_function("activityRequests.length === 3")
    resolve(page, 2, payload(count=0, ids=[]))
    expect(panel.get_by_test_id("header-activity-operation")).to_have_count(0)
    expect(panel).to_be_visible()
    assert len(calls) == 1 and calls[0][0] == "POST"
