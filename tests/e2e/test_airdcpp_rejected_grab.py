"""Rejected DC results use explicit approval without bypassing replacement review."""

from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
@pytest.mark.parametrize("owned", [False, True])
def test_rejected_dc_confirmation_cancel_retry_and_queue(
    authed_page, seeded_server, theme, width, owned
):
    from pullbox.ui.series_detail_routes import _templates

    candidate = SimpleNamespace(
        release=SimpleNamespace(
            title="New Thunderbolts 011.cbz", indexer_name="AirDC++", size_bytes=12000000
        ),
        validation=SimpleNamespace(
            rejection_reason="Numbered standard issue cannot satisfy a single-record collection target."
        ),
        route=SimpleNamespace(tth="A" * 39),
        metrics=SimpleNamespace(source_count=1, free_slots=8, total_slots=8),
    )
    html = (
        _templates()
        .get_template("partials/issue_dc_search_results.html")
        .render(
            outcome=SimpleNamespace(matched=(), rejected=(candidate,), partial=False),
            dc_rows=[],
            dc_rejected_rows=[{"candidate": candidate, "route_token": "t" * 40}],
            issue={"id": 1, "series_id": 1, "has_library_file": owned},
            csrf_token="test",
        )
    )
    page = authed_page
    page.set_viewport_size({"width": width, "height": 800})
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}');")
    page.route(
        "**/htmx/issues/1/search-results*",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    page.route(
        "**/htmx/issues/1/dc-search-status*", lambda route: route.fulfill(json={"available": False})
    )
    grabs = []

    def queue(route):
        grabs.append(route.request.post_data_json)
        if len(grabs) == 1:
            route.fulfill(status=409, json={"detail": "The selected AirDC++ client is not ready."})
        else:
            route.fulfill(status=201, json={"status": "sent"})

    page.route("**/api/v1/issues/1/dc-grab", queue)
    page.goto(f"{seeded_server}/issues/1?replacement=search")
    search = page.get_by_test_id("issue-search-modal")
    trigger = search.get_by_role("button", name="Grab anyway", exact=True)
    expect(trigger).to_be_visible()
    assert_no_axe_violations(
        page, name=f"airdc-results-{theme}-{owned}", include=[".issue-search-dc-results"]
    )
    trigger.click()
    confirm = page.get_by_role("dialog", name="Grab Rejected Result", exact=True)
    expect(
        confirm.get_by_text("File safety and metadata checks still apply", exact=False)
    ).to_be_visible()
    expect(confirm.get_by_text("Numbered standard issue", exact=False)).to_be_visible()
    assert grabs == []
    expect(confirm.locator(":scope > div")).to_have_css("opacity", "1")
    expect(confirm.locator(".bg-pb-card")).to_have_css("opacity", "1")
    assert_no_axe_violations(
        page, name=f"airdc-grab-{theme}-{owned}", include=["#pb-confirm-dialog"]
    )
    confirm.get_by_role("button", name="Cancel", exact=True).click()
    expect(search).to_be_visible()
    expect(trigger).to_be_enabled()
    assert grabs == []

    for attempt in range(2):
        trigger.click()
        confirm.get_by_role("button", name="Grab anyway", exact=True).click()
        if owned:
            replacement = page.get_by_role("dialog", name="Download replacement?", exact=True)
            if attempt == 0:
                replacement.get_by_role("button", name="Cancel", exact=True).click()
                expect(trigger).to_be_enabled()
                assert grabs == []
                trigger.click()
                confirm.get_by_role("button", name="Grab anyway", exact=True).click()
            replacement.get_by_role("button", name="Download replacement", exact=True).click()
        if attempt == 0:
            expect(
                page.get_by_text("The selected AirDC++ client is not ready.", exact=True)
            ).to_be_visible()
            expect(trigger).to_be_enabled()
            assert len(grabs) == 1

    queued = search.get_by_role("button", name="Queued", exact=True)
    expect(queued).to_be_disabled()
    expected = {"dc_route_token": "t" * 40, "override_rejection": True}
    if owned:
        expected["replace_existing_file"] = True
    assert grabs == [expected, expected]
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
