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
    page.evaluate("activityStream.onopen()")
    assert page.evaluate(
        "Alpine.$data(document.querySelector('[x-data=\"appShell()\"]')).activityStreamOpen"
    )
    yield page


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


def test_busy_stream_cannot_postpone_refresh_forever(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    for _ in range(6):
        page.evaluate("activityStream.listeners.progress()")
        page.clock.run_for(100)
    assert page.evaluate("activityRequests.length") == 2


def test_inflight_invalidation_is_replayed(activity_page):
    page = activity_page
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(500)
    page.evaluate("activityRequests[0].resolve({operations: [], active_count: 0})")
    page.wait_for_function("activityRequests.length === 2", timeout=2000)


def test_completion_expires_with_healthy_stream(activity_page):
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


def test_failed_fetch_retries_without_blanking_even_with_open_stream(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(500)
    page.evaluate("activityRequests[1].reject(new Error('offline'))")
    page.clock.run_for(3001)
    assert page.evaluate("activityRequests.length") == 3
    assert page.evaluate("Alpine.$data(document.body).activityOperations[0].id") == 1
    resolve(page, 2, payload(count=2, ids=[1, 2]))
    assert page.evaluate("Alpine.$data(document.body).activityBadgeCount()") == 2


def test_slow_fetch_times_out_and_destroy_cancels_refreshes(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(500)
    page.clock.run_for(15001)
    assert page.evaluate("activityRequests[1].aborted")
    page.clock.run_for(3001)
    assert page.evaluate("activityRequests.length") == 3
    page.evaluate("Alpine.$data(document.body).destroy()")
    assert page.evaluate("activityRequests[2].aborted")
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(20000)
    assert page.evaluate("activityRequests.length") == 3


def test_stream_reconnect_refreshes_and_stops_fallback_poll(activity_page):
    page = activity_page
    resolve(page, 0, payload())
    page.evaluate("activityStream.onerror()")
    assert page.evaluate("activityStream.closed")
    page.clock.run_for(3001)
    resolve(page, 1, payload(count=2, ids=[1, 2]))
    page.evaluate("activityStream.onopen(); activityStream.listeners.ready()")
    page.clock.run_for(501)
    assert page.evaluate("activityRequests.length") == 3
    resolve(page, 2, payload(count=3, ids=[1, 2, 3]))
    page.clock.run_for(10000)
    assert page.evaluate("activityRequests.length") == 3


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
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(501)
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
    page.evaluate("activityStream.listeners.progress()")
    page.clock.run_for(501)
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
