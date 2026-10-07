"""GCD API arcs use shared review controls; only outbound HTTP is synthetic."""

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.test_gcd_api_settings import gcd_flag as _gcd_flag
from tests.e2e.test_gcd_api_settings import gcd_page as _gcd_page
from tests.e2e.test_gcd_api_settings import save
from tests.unit.test_gcd_api_arcs import arc_row, envelope
from tests.unit.test_gcd_api_v2 import TOKEN, issue_row, series_row, source

pytestmark = pytest.mark.e2e
gcd_flag = _gcd_flag
gcd_page = _gcd_page


@pytest.fixture
def gcd_arc_wire(monkeypatch):
    state = {"failure": False}

    def handle(request):
        if request.url.path == "/api/v2/story-arcs/":
            return httpx.Response(200, json=envelope(request, [arc_row()]))
        if request.url.path == "/api/v2/story-arcs/4/":
            return httpx.Response(200, json=arc_row())
        if request.url.path == "/api/v2/story-arcs/4/issues/":
            return (
                httpx.Response(404)
                if state["failure"]
                else httpx.Response(
                    200, json=envelope(request, [issue_row(), issue_row(765610, "50-x")])
                )
            )
        assert request.url.path == "/api/v2/series/50494/"
        return httpx.Response(200, json=series_row())

    monkeypatch.setattr(
        "pullbox.providers.metadata.sources.GcdApiV2Source", lambda _: source(handle)
    )
    return state


def enable(page):
    card = page.get_by_test_id("gcd-api-access")
    card.get_by_label("Enable GCD API v2", exact=True).check()
    card.get_by_label("GCD API token", exact=True).fill(TOKEN)
    save(page)


@pytest.mark.parametrize(
    "theme,width,os_scheme",
    [
        ("light", 1280, "dark"),
        ("dark", 390, "light"),
        ("system", 1280, "light"),
        ("system", 390, "dark"),
    ],
)
def test_gcd_api_search_review_reorder_skip_and_accessibility(
    gcd_page, gcd_arc_wire, seeded_server, theme, width, os_scheme
):
    page = gcd_page
    enable(page)
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=os_scheme, reduced_motion="reduce")
    page.evaluate("theme => applyTheme(theme)", theme)
    page.goto(f"{seeded_server}/story-arcs/add?q=Civil+War&source=gcd_api_v2")
    expect(page.locator("html")).to_have_attribute(
        "data-theme", os_scheme if theme == "system" else theme
    )
    link = page.get_by_role("link", name="Preview Civil War", exact=True)
    expect(link).to_be_visible()
    assert_no_axe_violations(page, name=f"gcd-api-arc-search-{theme}-{width}", include=["#content"])
    link.click()
    page.wait_for_url("**/story-arcs/catalog/gcd_api_v2/4")
    expect(
        page.get_by_text("GCD publication order is a starting point", exact=False)
    ).to_be_visible()
    form = page.get_by_test_id("story-arc-catalog-add-form")
    rows = form.locator("[data-provider-issue-id]")
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-provider-issue-id", "765609")
    form.get_by_role("button", name="Move Badrock #1 down", exact=True).press("Enter")
    expect(rows.first).to_have_attribute("data-provider-issue-id", "765610")
    checkbox = form.get_by_role("checkbox", name="Skip #50-x in this arc", exact=True)
    checkbox.check()
    expect(checkbox).to_be_checked()
    expect(form.get_by_role("button", name="Add Story Arc", exact=True)).to_be_enabled()
    assert_no_axe_violations(page, name=f"gcd-api-arc-review-{theme}-{width}", include=["#content"])
    assert page.locator("#content").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    assert TOKEN not in page.content()


def test_unavailable_member_endpoint_prevents_add_and_retry_recovers(
    gcd_page, gcd_arc_wire, seeded_server
):
    page = gcd_page
    enable(page)
    gcd_arc_wire["failure"] = True
    page.goto(f"{seeded_server}/story-arcs/catalog/gcd_api_v2/4")
    surface = page.get_by_test_id("story-arc-catalog-preview")
    expect(surface).to_have_attribute("data-preview-ready", "false")
    writes = []
    page.on(
        "request", lambda request: writes.append(request.url) if request.method == "POST" else None
    )
    page.get_by_role("button", name="Add Story Arc", exact=True).click()
    expect(page.get_by_test_id("story-arc-preview-submit-error")).to_contain_text(
        "preview is incomplete"
    )
    assert not writes
    retry = page.get_by_role("button", name="Retry preview", exact=True)
    expect(retry).to_be_visible()
    gcd_arc_wire["failure"] = False
    retry.click()
    expect(surface).to_have_attribute("data-preview-ready", "true")
    expect(
        page.get_by_test_id("story-arc-catalog-add-form").locator("[data-provider-issue-id]")
    ).to_have_count(2)
