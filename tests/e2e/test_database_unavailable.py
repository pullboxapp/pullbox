"""An offline library must not turn a failed in-page action into a new navigation."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_offline_content_action_stays_on_page_without_retry(authed_page, seeded_server):
    url = f"{seeded_server}/series"
    authed_page.goto(url)
    methods = []
    message = "Pullbox cannot reach the library database. Check the status of your last action."

    def unavailable(route):
        methods.append(route.request.method)
        route.fulfill(
            status=503, json={"error": {"code": "DATABASE_UNAVAILABLE", "message": message}}
        )

    authed_page.route("**/probe-offline-action", unavailable)
    authed_page.evaluate(
        """() => { htmx.ajax("POST", "/probe-offline-action", {target:"#content"}); }"""
    )
    toast = authed_page.locator('#toast-container [data-toast-id="database-unavailable"]')
    expect(toast).to_contain_text(message)
    expect(authed_page).to_have_url(url)
    assert methods == ["POST"]
