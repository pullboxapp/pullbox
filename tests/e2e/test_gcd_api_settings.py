"""Flagged GCD access uses real policy saves; provider traffic stays offline."""

import pytest
from playwright.sync_api import expect

from pullbox.config import get_settings
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.pages.settings import SettingsPage

pytestmark = pytest.mark.e2e
TOKEN = "synthetic-browser-gcd-token"


@pytest.fixture
def gcd_flag(seeded_server, monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_GCD_API_V2_ENABLED", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def gcd_page(authed_page, seeded_server, gcd_flag):
    page = authed_page
    SettingsPage(page, seeded_server).goto("metadata")
    csrf = page.evaluate("readCsrfTokenFromBody()")
    endpoint = seeded_server + "/api/v1/metadata/sources/gcd_api_v2"
    original = next(
        row
        for row in page.request.get(seeded_server + "/api/v1/metadata/sources").json()
        if row["source"] == "gcd_api_v2"
    )
    assert not original["credential_configured"]
    yield page
    page.unroute_all(behavior="ignoreErrors")
    current = next(
        row
        for row in page.request.get(seeded_server + "/api/v1/metadata/sources").json()
        if row["source"] == "gcd_api_v2"
    )
    response = page.request.put(
        endpoint,
        data={
            **{
                key: original[key]
                for key in ("enabled", "priority", "domain_priorities", "settings")
            },
            "revision": current["revision"],
            "clear_credential": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.ok


def save(page):
    with page.expect_response("**/api/v1/metadata/sources/gcd_api_v2") as response:
        page.get_by_role("button", name="Save GCD API settings", exact=True).click()
    assert response.value.status == 200
    expect(page.get_by_test_id("gcd-api-access").get_by_role("status")).to_contain_text("saved")
    return response.value


def test_gcd_token_save_preserve_disable_and_explicit_clear(gcd_page):
    page = gcd_page
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    card = page.get_by_test_id("gcd-api-access")
    expect(card).to_be_visible()
    token = card.get_by_label("GCD API token", exact=True)
    assert token.get_attribute("type") == "password"
    card.get_by_label("Enable GCD API v2", exact=True).check()
    token.fill(TOKEN)
    response = save(page)
    assert response.json()["credential_configured"] is True
    assert TOKEN not in response.text()
    expect(token).to_have_value("")
    page.reload()
    expect(card.get_by_label("Enable GCD API v2", exact=True)).to_be_checked()
    expect(card.get_by_text("A token is saved.", exact=True)).to_be_visible()
    assert TOKEN not in page.content()
    card.get_by_label("Enable GCD API v2", exact=True).uncheck()
    assert save(page).json()["credential_configured"] is True
    card.get_by_label("Remove saved token", exact=True).check()
    expect(token).to_be_disabled()
    assert save(page).json()["credential_configured"] is False
    assert not any(url.endswith("/test") or "beta.comics.org" in url for url in requests)


def test_gcd_priority_and_token_drafts_do_not_overwrite_each_other(gcd_page):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    order = page.get_by_test_id("metadata-order-global")
    order.locator('[data-order-direction="down"]').first.click()
    draft = order.locator("[data-source-label]").all_text_contents()
    card.get_by_label("Enable GCD API v2", exact=True).check()
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    save(page)
    expect(order.locator("[data-source-label]")).to_have_text(draft)
    card.get_by_label("GCD API token", exact=True).fill(TOKEN + "-next")
    with page.expect_response("**/api/v1/metadata/priorities") as updated:
        page.get_by_role("button", name="Save metadata priority", exact=True).click()
    assert updated.value.status == 200
    expect(card.get_by_label("GCD API token", exact=True)).to_have_value(TOKEN + "-next")
    result = save(page).json()
    expected = next(row for row in updated.value.json() if row["source"] == "gcd_api_v2")
    assert result["priority"] == expected["priority"]
    assert result["revision"] == expected["revision"] + 1


def test_gcd_errors_do_not_echo_token_or_replace_pending_buttons(gcd_page):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    token = card.get_by_label("GCD API token", exact=True)
    token.fill(TOKEN)
    page.route(
        "**/api/v1/metadata/sources/gcd_api_v2",
        lambda route: route.fulfill(status=500, json={"detail": TOKEN}),
    )
    button = card.get_by_role("button", name="Save GCD API settings", exact=True)
    button.click()
    expect(card.get_by_role("alert")).to_contain_text("Could not save")
    expect(
        card.get_by_role("button", name="Load saved GCD API settings", exact=True)
    ).to_be_visible()
    assert TOKEN not in card.inner_text()
    expect(token).to_have_value(TOKEN)
    page.unroute_all(behavior="ignoreErrors")
    page.evaluate("""() => {
      const fetch = window.fetch.bind(window);
      window.fetch = (url, options) => url.endsWith('/api/v1/metadata/sources/gcd_api_v2')
        ? new Promise(() => {}) : fetch(url, options);
    }""")
    button.evaluate("node => { node.retained = true; }")
    button.click()
    expect(button).to_be_disabled()
    assert button.evaluate("node => node.retained")
    expect(card.get_by_label("Enable GCD API v2", exact=True)).to_be_disabled()


def test_gcd_requires_token_and_disable_before_removing_saved_token(gcd_page):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    writes = []
    page.on(
        "request", lambda request: writes.append(request.url) if request.method == "PUT" else None
    )
    card.get_by_label("Enable GCD API v2", exact=True).check()
    card.get_by_role("button", name="Save GCD API settings", exact=True).click()
    expect(card.get_by_role("alert")).to_contain_text("Enter a token before enabling")
    assert not writes
    card.get_by_label("GCD API token", exact=True).fill("bad token")
    card.get_by_role("button", name="Save GCD API settings", exact=True).click()
    expect(card.get_by_role("alert")).to_contain_text("without spaces")
    assert not writes
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    save(page)
    writes.clear()
    card.get_by_label("Remove saved token", exact=True).check()
    card.get_by_role("button", name="Save GCD API settings", exact=True).click()
    expect(card.get_by_role("alert")).to_contain_text("Disable GCD API v2 before removing")
    assert not writes


def test_gcd_stale_save_requires_explicit_reload(gcd_page, seeded_server):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    policies = page.request.get(seeded_server + "/api/v1/metadata/sources").json()
    csrf = page.evaluate("readCsrfTokenFromBody()")
    response = page.request.put(
        seeded_server + "/api/v1/metadata/priorities",
        data={
            "order": [row["source"] for row in policies],
            "revisions": {row["source"]: row["revision"] for row in policies},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.ok
    card.get_by_label("Enable GCD API v2", exact=True).check()
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    with page.expect_response("**/api/v1/metadata/sources/gcd_api_v2") as saved:
        card.get_by_role("button", name="Save GCD API settings", exact=True).click()
    assert saved.value.status == 409
    expect(card.get_by_role("alert")).to_contain_text("changed in another session")
    expect(card.get_by_label("GCD API token", exact=True)).to_have_value(TOKEN)
    card.get_by_role("button", name="Load saved GCD API settings", exact=True).click()
    expect(card.get_by_label("GCD API token", exact=True)).to_have_value("")
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    assert save(page).json()["credential_configured"]


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_gcd_access_uses_standard_buttons_and_accessible_layout(gcd_page, theme, width):
    page = gcd_page
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate(
        "theme => { localStorage.setItem('theme', theme); document.documentElement.setAttribute('data-theme', theme); }",
        theme,
    )
    card = page.get_by_test_id("gcd-api-access")
    expect(card).to_be_visible()
    assert "btn-primary" in card.get_by_role(
        "button", name="Save GCD API settings", exact=True
    ).get_attribute("class")
    assert_no_axe_violations(
        page, name=f"gcd-api-{theme}", include=['[data-testid="gcd-api-access"]']
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_gcd_access_is_hidden_when_flag_is_disabled(authed_page, seeded_server, monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_GCD_API_V2_ENABLED", "false")
    get_settings.cache_clear()
    try:
        SettingsPage(authed_page, seeded_server).goto("metadata")
        expect(authed_page.get_by_test_id("gcd-api-access")).to_have_count(0)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
