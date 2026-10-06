"""Flagged GCD access uses real policy saves; provider traffic stays offline."""

import httpx
import pytest
from playwright.sync_api import expect

from pullbox.config import get_settings
from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.providers.metadata.gcd_api_v2 import GcdApiV2Source
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.pages.settings import SettingsPage
from tests.unit.test_gcd_api_v2 import series_row

pytestmark = pytest.mark.e2e
TOKEN = "synthetic-browser-gcd-token"


@pytest.fixture
def sign_in_wire(monkeypatch):
    from pullbox.providers.metadata import gcd_api_v2, sources

    original = gcd_api_v2.exchange_gcd_token

    def handler(request):
        if request.method == "POST":
            assert request.url == "https://beta.comics.org/api/v2/auth/token/"
            return httpx.Response(200, json={"token": TOKEN})
        assert request.headers["authorization"] == f"Token {TOKEN}"
        return httpx.Response(200, json={"count": 1, "next": None, "results": [series_row()]})

    async def exchange(*args):
        return await original(
            *args, transport=httpx.MockTransport(handler), cooldown=ProviderCooldown()
        )

    monkeypatch.setattr(gcd_api_v2, "exchange_gcd_token", exchange)
    monkeypatch.setattr(
        sources,
        "GcdApiV2Source",
        lambda token: GcdApiV2Source(
            token, transport=httpx.MockTransport(handler), cooldown=ProviderCooldown()
        ),
    )


def fill_sign_in(card):
    card.get_by_text("Sign in with GCD instead", exact=True).click()
    card.get_by_label("GCD username", exact=True).fill("synthetic-browser-account")
    card.get_by_label("GCD password", exact=True).fill("synthetic-browser-password")


def test_gcd_sign_in_clears_credentials_immediately_and_retains_button(gcd_page):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    card.get_by_text("Sign in with GCD instead", exact=True).click()
    username = card.get_by_label("GCD username", exact=True)
    password = card.get_by_label("GCD password", exact=True)
    username.fill("synthetic-browser-account")
    password.fill("synthetic-browser-password")
    page.evaluate("""() => {
      const fetch = window.fetch.bind(window);
      window.fetch = (url, options) => url.endsWith('/gcd_api_v2/sign-in')
        ? new Promise(() => {}) : fetch(url, options);
    }""")
    button = card.get_by_role("button", name="Sign in and enable GCD", exact=True)
    button.evaluate("node => { node.retained = true; }")
    button.click()
    expect(username).to_have_value("")
    expect(password).to_have_value("")
    expect(button).to_be_disabled()
    assert button.evaluate("node => node.retained")


def test_gcd_sign_in_saves_a_verified_token_and_preserves_a_priority_draft(gcd_page, sign_in_wire):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    order = page.get_by_test_id("metadata-order-global")
    order.locator('[data-order-direction="down"]').first.click()
    draft = order.locator("[data-source-label]").all_text_contents()
    fill_sign_in(card)
    with page.expect_response("**/gcd_api_v2/sign-in") as response:
        card.get_by_role("button", name="Sign in and enable GCD", exact=True).click()
    assert response.value.ok
    saved = response.value.json()
    assert saved["enabled"] and saved["credential_configured"] and saved["last_status"] == "ok"
    expect(card.get_by_role("status")).to_contain_text("verified token is saved")
    expect(card.get_by_label("Enable GCD API v2", exact=True)).to_be_checked()
    expect(card.get_by_label("GCD username", exact=True)).to_have_value("")
    expect(card.get_by_label("GCD password", exact=True)).to_have_value("")
    expect(order.locator("[data-source-label]")).to_have_text(draft)
    assert TOKEN not in page.content()
    with page.expect_response("**/api/v1/metadata/priorities") as priority:
        page.get_by_role("button", name="Save metadata priority", exact=True).click()
    assert priority.value.ok
    result = next(row for row in priority.value.json() if row["source"] == "gcd_api_v2")
    assert result["enabled"] and result["credential_configured"]
    assert result["revision"] == saved["revision"] + 1


@pytest.mark.parametrize("status", [400, 429, 502, 504, 409, 500])
def test_gcd_sign_in_failure_is_safe_and_clears_fields(gcd_page, status):
    page = gcd_page
    card = page.get_by_test_id("gcd-api-access")
    fill_sign_in(card)
    page.route(
        "**/gcd_api_v2/sign-in",
        lambda route: route.fulfill(status=status, json={"detail": "synthetic-browser-password"}),
    )
    card.get_by_role("button", name="Sign in and enable GCD", exact=True).click()
    expect(card.get_by_role("alert")).to_be_visible()
    expect(card.get_by_label("GCD username", exact=True)).to_have_value("")
    expect(card.get_by_label("GCD password", exact=True)).to_have_value("")
    assert "synthetic-browser-password" not in card.inner_text()
    assert TOKEN not in page.content()
    if status in {409, 500}:
        card.get_by_role("button", name="Load saved GCD API settings", exact=True).click()
        expect(card.get_by_role("alert")).not_to_be_visible()


def test_gcd_sign_in_disclosure_clears_drafts_and_cannot_overwrite_api_drafts(gcd_page):
    card = gcd_page.get_by_test_id("gcd-api-access")
    fill_sign_in(card)
    card.get_by_text("Sign in with GCD instead", exact=True).click()
    card.get_by_text("Sign in with GCD instead", exact=True).click()
    expect(card.get_by_label("GCD username", exact=True)).to_have_value("")
    expect(card.get_by_label("GCD password", exact=True)).to_have_value("")
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    expect(card.get_by_label("GCD username", exact=True)).to_be_disabled()
    expect(card.get_by_role("button", name="Sign in and enable GCD", exact=True)).to_be_disabled()
    expect(
        card.get_by_text("Save or reload your pending API settings changes before signing in.")
    ).to_be_visible()


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
    card.get_by_text("Sign in with GCD instead", exact=True).click()
    assert "btn-primary" in card.get_by_role(
        "button", name="Sign in and enable GCD", exact=True
    ).get_attribute("class")
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
