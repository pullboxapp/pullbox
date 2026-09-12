"""Mocked-provider browser coverage for reviewed Story Arc adoption and refresh."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.story_arc_file_helpers import configure_arc_file_defaults
from tests.story_arc_catalog_fixtures import CatalogProvider

pytestmark = pytest.mark.e2e


@pytest.fixture
def catalog_provider(monkeypatch: pytest.MonkeyPatch) -> CatalogProvider:
    provider = CatalogProvider()
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key", AsyncMock(return_value="test")
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider", lambda **_: provider
    )
    return provider


def test_catalog_search_uses_standard_comicvine_loading_popup(
    authed_page: Page, seeded_server: str
) -> None:
    page = authed_page
    held_routes: list[Route] = []

    def hold_search(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query).get("q", [""])[0]
        if query:
            held_routes.append(route)
            return
        route.continue_()

    page.route("**/story-arcs/add**", hold_search)
    page.goto(f"{seeded_server}/story-arcs/add", wait_until="networkidle")
    query = page.get_by_label("Comic Vine arc name")
    query.fill("Numbering")
    query.press("Enter")

    indicator = page.get_by_test_id("story-arc-add-results-loading")
    expect(indicator).to_be_visible()
    expect(indicator).to_have_attribute("aria-live", "polite")
    expect(indicator).to_have_attribute("data-comicvine-search-loading-contract", "v1")
    expect(indicator.get_by_text("Searching ComicVine", exact=True)).to_be_visible()
    expect(indicator.get_by_text("Large catalogs can take a moment.", exact=True)).to_be_visible()
    spinner = indicator.locator("svg").first
    expect(spinner).to_have_css("width", "20px")
    expect(spinner).to_have_css("height", "20px")
    assert held_routes

    held_routes.pop().fulfill(
        status=200,
        content_type="text/html",
        body=(
            '<section id="story-arc-add-results" data-testid="story-arc-add-results" '
            'class="add-series-results-shell space-y-3"></section>'
        ),
    )
    page.unroute("**/story-arcs/add**")


def test_catalog_results_use_add_series_card_layout(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 1000})
    response = page.goto(f"{seeded_server}/story-arcs/add?q=event")
    assert response is not None and response.status == 200
    results = page.get_by_test_id("story-arc-catalog-results")
    card = results.locator(".add-series-result-card").first
    title = card.locator(".add-series-result-title").bounding_box()
    meta = card.locator(".add-series-result-meta").bounding_box()
    action = card.get_by_role("link", name="Preview Numbering Event").bounding_box()
    bounds = card.bounding_box()
    assert title and meta and action and bounds
    assert meta["y"] >= title["y"] + title["height"], "Metadata belongs below the title"
    assert abs(bounds["x"] + bounds["width"] - action["x"] - action["width"] - 15) < 2
    expect(card.locator(".add-series-result-description")).to_have_text(
        "A test event across multiple comic series."
    )
    expect(card).not_to_have_class(re.compile(r"add-series-result-card-static"))
    expect(card.locator(".add-series-result-meta")).to_have_text("Fixture Publisher 2 issues")
    expect(results.get_by_text("2 matching Story Arcs", exact=True)).to_have_count(0)
    expect(results.locator("nav")).to_have_count(0)
    unknown = results.locator(".add-series-result-card").nth(1)
    expect(unknown).not_to_contain_text("Publisher unknown")
    expect(unknown).not_to_contain_text("Unknown reported members")

    output = Path("test-results/story-arc-search")
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        toggle = page.get_by_test_id("header-theme-toggle")
        target = f"Switch to {theme} mode"
        if toggle.get_attribute("aria-label") != target:
            toggle.click()
        page.get_by_role("button", name=target, exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        assert_no_axe_violations(page, name=f"arc-search-{theme}", include=["#content"])
        page.screenshot(path=str(output / f"{theme}.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    assert page.locator("#content").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    expect(card.get_by_role("link", name="Preview Numbering Event")).to_be_visible()
    assert_no_axe_violations(page, name="arc-search-mobile", include=["#content"])
    page.screenshot(path=str(output / "mobile.png"), full_page=True)


def test_keyboard_catalog_add_and_refresh_preserve_reviewed_order(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    configure_arc_file_defaults(page, seeded_server, prefix=True)
    page.goto(f"{seeded_server}/story-arcs/add", wait_until="domcontentloaded")
    query = page.get_by_label("Comic Vine arc name")
    query.fill("Numbering")
    cover = page.get_by_role("img", name="Numbering Event")
    expect(cover).to_be_visible()
    expect(cover).to_have_attribute("src", "https://example.test/story-arcs/42.jpg")
    expect(page.get_by_role("link", name="Preview Numbering Event")).to_be_visible()
    query.press("Tab")
    page.get_by_role("link", name="Preview Numbering Event").focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/story-arcs/catalog/42")
    expect(
        page.get_by_text("Issues are listed in Comic Vine's returned order.", exact=False)
    ).to_be_visible()
    form = page.get_by_test_id("story-arc-catalog-add-form")
    form.get_by_role("button", name="Move Exact Comics #1000000 down", exact=True).press("Enter")
    expect(form.locator("[data-provider-issue-id]").first).to_have_attribute(
        "data-provider-issue-id", "102"
    )
    canonical_root = form.get_by_label("Library root for new series")
    canonical_root.click()
    page.get_by_role("option").nth(1).click()
    expect(form.get_by_label("Separate folder")).to_have_count(0)
    expect(form.get_by_test_id("story-arc-create-storage")).to_contain_text(
        "Copy issues into arc folders"
    )
    expect(form.get_by_label("I reviewed the reading order")).to_have_count(0)
    assert_no_axe_violations(
        page, name="story-arc-catalog-review", include=["[data-testid='story-arc-catalog-preview']"]
    )
    form.get_by_role("button", name="Add Story Arc", exact=True).click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=catalog-added$"))
    arc_url = page.url.split("?", 1)[0]
    page.goto(f"{arc_url}?per_page=1", wait_until="networkidle")
    rows = page.locator('[data-testid="story-arc-reading-order-table"] tbody')
    moved_id = rows.first.get_attribute("data-membership-id")
    assert moved_id
    held_moves: list[Route] = []
    page.route("**/memberships/*/move", lambda route: held_moves.append(route))
    page.evaluate(
        "window.reorderShell = document.querySelector('#content'); window.reorderTable = document.querySelector('[data-testid=story-arc-reading-order-table]')"
    )
    down = rows.first.get_by_role("button", name="Move issue 1AU down", exact=True)
    down.press("Enter")
    expect(down).to_be_disabled()
    expect(rows.first.get_by_role("button", name="Move issue 1AU up", exact=True)).to_be_disabled()
    assert len(held_moves) == 1
    held_moves[0].continue_()
    page.unroute("**/memberships/*/move")
    page.wait_for_url(re.compile(r"page=2"))
    expect(rows.first).to_have_attribute("data-membership-id", moved_id)
    up = rows.first.get_by_role("button", name="Move issue 1AU up", exact=True)
    expect(up).to_be_focused()
    expect(page.get_by_test_id("story-arc-reorder-preview")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-placement-preview")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-placement-state")).to_have_count(0)
    assert page.evaluate(
        "window.reorderShell === document.querySelector('#content') && window.reorderTable === document.querySelector('[data-testid=story-arc-reading-order-table]')"
    )
    with page.expect_response(re.compile(r"/memberships/\d+/move$")) as moved_back:
        up.press("Enter")
    assert moved_back.value.status == 200
    page.wait_for_url(re.compile(r"[?&]page=1(&|$)"))
    expect(rows.first).to_have_attribute("data-membership-id", moved_id)
    expect(rows.first.get_by_role("button", name="Move issue 1AU down", exact=True)).to_be_focused()
    page.route("**/memberships/*/move", lambda route: route.fulfill(status=503, body="Unavailable"))
    rows.first.get_by_role("button", name="Move issue 1AU down", exact=True).click()
    expect(
        page.get_by_role("alert").filter(has_text="The new order could not be confirmed")
    ).to_be_visible()
    expect(rows.first.get_by_role("button", name="Move issue 1AU down", exact=True)).to_be_enabled()
    expect(rows.first).to_have_attribute("data-membership-id", moved_id)
    page.unroute("**/memberships/*/move")
    assert_no_axe_violations(
        page,
        name="story-arc-saved-order",
        include=["[data-testid='story-arc-detail-reading-order-section']"],
    )
    page.goto(arc_url, wait_until="networkidle")
    expect(page.get_by_test_id("story-arc-edit-form")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-add-membership-form")).to_have_count(0)
    expect(page.locator('[data-testid^="story-arc-remove-membership-"]')).to_have_count(0)
    expect(page.get_by_label("Include upcoming issues")).to_have_count(0)
    monitor = page.get_by_role("switch", name="Toggle monitoring for this Story Arc")
    expect(monitor).to_be_visible()
    # Boosted navigation updates the URL before HTMX's settle pass binds the
    # new form. A keyboard press must wait for that pass as well as visibility.
    expect(page.locator("body")).not_to_have_attribute("data-shell-pending", "")
    expect(page.locator("#content .htmx-added, #content.htmx-settling")).to_have_count(0)
    page.wait_for_load_state("load")
    assert errors == []
    help_text = (
        "Monitoring checks for new members and searches missing issues when released. "
        "Parent series monitoring stays unchanged."
    )
    help_description = page.locator("#story-arc-monitor-help")
    expect(help_description).to_have_class("sr-only")
    expect(monitor).to_have_attribute("aria-describedby", "story-arc-monitor-help")
    control = page.get_by_test_id("story-arc-action-monitor-control")
    tooltip = page.locator("#global-tooltip-host .app-tooltip-overlay")
    control.locator(".toggle-switch").hover()
    expect(tooltip).to_be_visible()
    expect(tooltip).to_have_text(help_text)
    page.mouse.move(0, 0)
    expect(tooltip).not_to_be_visible()
    monitor.focus()
    expect(tooltip).to_be_visible()
    expect(tooltip).to_have_text(help_text)
    monitor.press("Escape")
    expect(tooltip).not_to_be_visible()
    expect(monitor).not_to_be_checked()
    assert_no_axe_violations(
        page, name="story-arc-monitor-tooltip", include=["[data-testid='story-arc-detail-hero']"]
    )
    with (
        page.expect_navigation(wait_until="load"),
        page.expect_response(lambda response: "/monitor" in response.url) as monitored,
    ):
        monitor.press("Space")
    assert monitored.value.status in (204, 303), monitored.value.text()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=updated$"))
    expect(monitor).to_be_checked()
    with page.expect_navigation(wait_until="load"):
        monitor.press("Space")
    expect(monitor).not_to_be_checked()
    members = page.locator("[data-membership-id]")
    expect(members.nth(0)).to_have_attribute("data-exact-issue-number", "1AU")
    expect(members.nth(1)).to_have_attribute("data-exact-issue-number", "1000000")
    expect(page.get_by_label("Issue file template")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-arc-files-summary")).to_contain_text(
        "Copied to arc folder"
    )
    expect(page.get_by_role("link", name="Open issue 1AU")).to_have_attribute(
        "href", re.compile(r"/issues/\d+\?source=story-arc&story_arc_id=\d+")
    )
    held: list[Route] = []
    page.route("**/story-arcs/*/catalog-refresh", lambda route: held.append(route))
    check = page.get_by_test_id("story-arc-action-provider-review")
    expect(check).to_have_accessible_name("Check for updates")
    with page.expect_request("**/story-arcs/*/catalog-refresh"):
        check.press("Enter")
    expect(check).to_have_attribute("aria-busy", "true")
    expect(check).to_contain_text("Checking…")
    expect(check.locator("svg")).to_have_class(re.compile("animate-spin"))
    assert len(held) == 1
    held[0].abort("failed")
    expect(check).to_have_attribute("aria-busy", "false")
    expect(check).to_have_accessible_name("Check for updates")
    with page.expect_request("**/story-arcs/*/catalog-refresh"):
        check.press("Enter")
    expect(check).to_have_attribute("aria-busy", "true")
    assert len(held) == 2
    held[1].continue_()
    page.unroute("**/story-arcs/*/catalog-refresh")
    expect(page.get_by_text("This story arc is up to date", exact=True)).to_be_visible()
    expect(page.get_by_label("I reviewed these provider changes")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-update-footer-dock")).to_be_visible()
    assert_no_axe_violations(
        page,
        name="story-arc-updates-current",
        include=["[data-testid='story-arc-catalog-refresh']"],
    )

    # Provider failures and incomplete responses must never look like no changes.
    catalog_provider.fail = True
    page.get_by_role("link", name="Check again", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("couldn't load this arc")
    expect(page.get_by_text("This story arc is up to date", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("story-arc-update-results")).to_have_count(0)
    catalog_provider.fail = False
    catalog_provider.metadata = replace(catalog_provider.metadata, membership_complete=False)
    page.get_by_role("link", name="Check again", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Incomplete member list")
    expect(page.get_by_label("I reviewed these provider changes")).to_have_count(0)
    catalog_provider.metadata = replace(catalog_provider.metadata, membership_complete=True)
    page.get_by_role("navigation", name="Breadcrumb").get_by_role(
        "link", name="Numbering Event", exact=True
    ).click()
    expect(page.get_by_test_id("story-arc-detail-page")).to_be_visible()
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=("101", "103")
    )
    page.get_by_role("link", name="Check for updates", exact=True).click()
    expect(page.get_by_text("Comic Vine issue ID 102 — preserved")).to_be_visible()
    additions = page.get_by_test_id("story-arc-update-additions")
    removals = page.get_by_test_id("story-arc-update-removals")
    page.set_viewport_size({"width": 1440, "height": 1000})
    left, right = additions.bounding_box(), removals.bounding_box()
    assert left and right and abs(left["y"] - right["y"]) < 2
    assert right["x"] >= left["x"] + left["width"]
    assert_no_axe_violations(
        page,
        name="story-arc-updates-changes",
        include=["[data-testid='story-arc-catalog-refresh']"],
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    left, right = additions.bounding_box(), removals.bounding_box()
    assert left and right and right["y"] >= left["y"] + left["height"]
    assert page.locator("#content").evaluate("el => el.scrollWidth <= el.clientWidth")
    assert_no_axe_violations(
        page, name="story-arc-updates-narrow", include=["[data-testid='story-arc-catalog-refresh']"]
    )
    # System theme exposes the dark-mode action first, even on a dark OS.
    theme_toggle = page.get_by_test_id("header-theme-toggle")
    if theme_toggle.get_attribute("aria-label") == "Switch to dark mode":
        theme_toggle.click()
    page.get_by_role("button", name="Switch to light mode").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert_no_axe_violations(
        page, name="story-arc-updates-light", include=["[data-testid='story-arc-catalog-refresh']"]
    )
    page.get_by_role("button", name="Switch to dark mode").click()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.get_by_label("I reviewed these provider changes").check()
    page.get_by_role("button", name="Save reviewed changes").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=catalog-refreshed$"))
    expect(page.locator("[data-membership-id]")).to_have_count(3)
    expect(page.locator("[data-membership-id]").nth(0)).to_have_attribute(
        "data-exact-issue-number", "1AU"
    )
    page.get_by_role("button", name="Review issue 2 match").click()
    expect(page.get_by_role("button", name="Confirm reading order")).to_be_visible()
    expect(page.get_by_role("button", name="Search local issues")).to_have_count(0)
    page.get_by_role("button", name="Confirm reading order").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?.*notice=resolved.*$"))
    expect(page.get_by_role("button", name="Review issue 2 match")).to_have_count(0)
    page.goto(f"{seeded_server}/story-arcs/add")
    page.get_by_label("Comic Vine arc name").fill("Numbering")
    expect(page.get_by_test_id("story-arc-existing-title-link")).to_have_attribute(
        "href", arc_url.removeprefix(seeded_server)
    )
    expect(page.get_by_test_id("story-arc-existing-cover-link")).to_have_attribute(
        "href", arc_url.removeprefix(seeded_server)
    )
    expect(page.get_by_test_id("story-arc-result-card").first).to_contain_text("In Library")
    assert errors == []


def test_catalog_add_is_enabled_and_explains_missing_root(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    submitted: list[str] = []
    page.on(
        "request",
        lambda request: submitted.append(request.url) if request.method == "POST" else None,
    )
    page.goto(f"{seeded_server}/story-arcs/catalog/80")
    form = page.get_by_test_id("story-arc-catalog-add-form")
    add = form.locator('button[type="submit"]')
    expect(add).to_be_enabled()
    expect(add).to_have_text("Add Story Arc")
    expect(form.get_by_label("I reviewed the reading order")).to_have_count(0)
    add.click()
    error = page.get_by_test_id("story-arc-preview-submit-error")
    expect(error).to_have_text("Choose a library root for new series before adding this Story Arc.")
    expect(error).to_be_focused()
    assert submitted == []
    expect(add).to_be_enabled()

    page.get_by_label("Library root for new series").click()
    page.get_by_role("option").nth(1).click()
    expect(error).not_to_be_visible()
    held: list[Route] = []
    page.route("**/story-arcs/catalog/80", lambda route: held.append(route))
    with page.expect_request("**/story-arcs/catalog/80"):
        add.click()
    expect(add).to_be_disabled()
    expect(page.get_by_role("button", name="Retry preview")).to_be_disabled()
    assert len(held) == 1
    assert held[0].request.method == "POST"
    held.pop().fulfill(status=503, body="Temporarily unavailable")
    expect(add).to_be_enabled()


def test_catalog_add_explains_no_available_roots(
    authed_page: Page,
    seeded_server: str,
    catalog_provider: CatalogProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullbox.ui.story_arc_catalog_routes.load_story_arc_placement_roots",
        AsyncMock(return_value=((), False)),
    )
    page = authed_page
    page.goto(f"{seeded_server}/story-arcs/catalog/81")
    add = page.get_by_role("button", name="Add Story Arc", exact=True)
    expect(add).to_be_enabled()
    add.click()
    error = page.get_by_test_id("story-arc-preview-submit-error")
    expect(error).to_contain_text("No managed library root is available")
    expect(error).to_contain_text("Settings > Media Management > Library roots")
    expect(error).to_be_focused()
    expect(add).to_be_enabled()


def test_catalog_partial_failure_and_retry_are_visible(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    catalog_provider.metadata = replace(
        catalog_provider.metadata, membership_complete=False, declared_issue_count=3
    )
    page.goto(f"{seeded_server}/story-arcs/catalog/99")
    expect(page.get_by_role("alert")).to_contain_text("Incomplete member list")
    add = page.get_by_role("button", name="Add Story Arc", exact=True)
    expect(add).to_be_enabled()
    submitted: list[str] = []
    page.on(
        "request",
        lambda request: submitted.append(request.url) if request.method == "POST" else None,
    )
    add.click()
    expect(page.get_by_test_id("story-arc-preview-submit-error")).to_contain_text(
        "Retry preview to load all issues"
    )
    assert submitted == []
    catalog_provider.fail = True
    page.get_by_role("button", name="Retry preview").click()
    expect(page.get_by_test_id("story-arc-preview-submit-error")).not_to_be_visible()
    expect(page.get_by_role("alert")).to_contain_text("couldn't load this arc")
    catalog_provider.fail = False
    catalog_provider.metadata = replace(
        catalog_provider.metadata, membership_complete=True, declared_issue_count=2
    )
    page.get_by_role("button", name="Retry preview").click()
    expect(page.get_by_test_id("story-arc-catalog-add-form")).to_be_visible()
    page.set_viewport_size({"width": 640, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    assert_no_axe_violations(
        page, name="story-arc-catalog-narrow", include=["[data-testid='story-arc-catalog-preview']"]
    )


def test_preview_pagination_retry_and_add_keep_all_page_choices(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    catalog_provider.metadata = replace(
        catalog_provider.metadata,
        issue_provider_ids=tuple(str(number) for number in range(101, 152)),
        declared_issue_count=51,
        title="Paged Event",
    )
    errors: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{seeded_server}/story-arcs/catalog/77")
    workspace = page.get_by_test_id("story-arc-catalog-preview")
    table = page.get_by_test_id("story-arc-preview-table")
    expect(table.locator("tr[data-provider-issue-id]")).to_have_count(25)
    workspace.evaluate("element => element.previewIdentity = 'preserved'")
    expect(table.get_by_role("spinbutton")).to_have_count(0)
    first = table.locator('[data-provider-issue-id="101"]')
    expect(
        first.get_by_role("button", name="Move Exact Comics #1000000 up", exact=True)
    ).to_be_disabled()
    first.get_by_role("button", name="Move Exact Comics #1000000 down", exact=True).press("Enter")
    expect(first.locator("[data-reading-position]")).to_have_text("2")
    expect(
        first.get_by_role("button", name="Move Exact Comics #1000000 down", exact=True)
    ).to_be_focused()
    expect(table.locator("[data-provider-issue-id]").first).to_have_attribute(
        "data-provider-issue-id", "102"
    )
    page.get_by_label("Skip #1AU in this arc", exact=True).check()
    page.get_by_label("Library root for new series").click()
    page.get_by_role("option").nth(1).click()
    page.get_by_role("switch", name="Monitor this story arc").press("Space")
    # The last visible row can move down across the page boundary. Follow it,
    # retain keyboard focus, and allow moving straight back to the previous page.
    boundary = table.locator('[data-provider-issue-id="125"]')
    boundary.get_by_role("button", name="Move Exact Comics #24 down", exact=True).press("Enter")
    expect(boundary.locator("[data-reading-position]")).to_have_text("26")
    expect(
        boundary.get_by_role("button", name="Move Exact Comics #24 down", exact=True)
    ).to_be_focused()
    expect(table.locator("[data-provider-issue-id]").first).to_have_attribute(
        "data-provider-issue-id", "125"
    )
    boundary.get_by_role("button", name="Move Exact Comics #24 up", exact=True).press("Enter")
    expect(boundary.locator("[data-reading-position]")).to_have_text("25")
    expect(table.locator("[data-provider-issue-id]").last).to_have_attribute(
        "data-provider-issue-id", "125"
    )
    boundary.get_by_role("button", name="Move Exact Comics #24 down", exact=True).click()
    page.get_by_label("Skip #26 in this arc", exact=True).check()
    page.get_by_test_id("series-pagination-prev").click()
    expect(first.locator("[data-reading-position]")).to_have_text("2")
    expect(page.get_by_label("Skip #1AU in this arc", exact=True)).to_be_checked()
    # Paging is entirely local, without fetching the provider or replacing the shell.
    assert sum(url.endswith("/story-arcs/catalog/77") for url in requests) == 1
    assert workspace.evaluate("element => element.previewIdentity") == "preserved"

    page.get_by_role("button", name="Retry preview").click()
    expect(workspace.get_by_role("status")).to_contain_text(
        "Your reading order, skips, and settings were kept"
    )
    expect(first.locator("[data-reading-position]")).to_have_text("2")
    expect(page.get_by_role("switch", name="Monitor this story arc")).to_be_checked()
    catalog_provider.fail = True
    page.get_by_role("button", name="Retry preview").click()
    expect(page.get_by_role("alert")).to_contain_text("couldn't load this arc")
    expect(first.locator("[data-reading-position]")).to_have_text("2")
    expect(page.get_by_role("button", name="Add Story Arc", exact=True)).to_be_enabled()
    catalog_provider.fail = False
    catalog_provider.metadata = replace(
        catalog_provider.metadata,
        issue_provider_ids=tuple(str(number) for number in range(101, 153) if number != 103),
    )
    page.get_by_role("button", name="Retry preview").click()
    expect(workspace.get_by_role("status")).to_contain_text("1 added, 1 no longer listed")
    expect(first.locator("[data-reading-position]")).to_have_text("2")
    assert workspace.evaluate("element => element.previewIdentity") == "preserved"
    page.get_by_label("Items per page").click()
    page.get_by_role("option", name="100", exact=True).click()
    expect(table.locator("tr[data-provider-issue-id]")).to_have_count(51)
    expect(boundary.locator("[data-reading-position]")).to_have_text("25")
    expect(page.get_by_label("Skip #26 in this arc", exact=True)).to_be_checked()
    expect(table.locator('[data-provider-issue-id="152"] [data-reading-position]')).to_have_text(
        "51"
    )
    expect(
        page.get_by_role("button", name="Move Exact Comics #51 down", exact=True)
    ).to_be_disabled()
    expect(table.locator("[data-reading-position]")).to_have_text(
        [str(number) for number in range(1, 52)]
    )
    expect(page.get_by_test_id("page-dock-pagination")).not_to_be_visible()
    page.get_by_role("switch", name="Monitor this story arc").press("Space")
    with page.expect_request(
        lambda request: request.method == "POST" and "/story-arcs/catalog/77" in request.url
    ) as submitted:
        page.get_by_role("button", name="Add Story Arc", exact=True).click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=catalog-added$"))
    body = submitted.value.post_data or ""
    assert body.count('name="issue_provider_ids"') == 51
    assert body.count('name="skipped_issue_provider_ids"') == 2
    submitted_ids = re.findall(r'name="issue_provider_ids"\r\n\r\n([^\r]+)', body)
    assert submitted_ids[:2] == ["102", "101"]
    assert submitted_ids[23:25] == ["126", "125"]
    assert submitted_ids[-1] == "152" and "103" not in submitted_ids
    assert re.findall(r'name="reading_orders"\r\n\r\n([^\r]+)', body) == [
        str(number) for number in range(1, 52)
    ]
    assert re.findall(r'name="skipped_issue_provider_ids"\r\n\r\n([^\r]+)', body) == ["102", "127"]
    assert 'name="order_reviewed"' not in body
    assert errors == []


def test_preview_reorder_single_member_and_pending_retry_are_disabled(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=("101",), declared_issue_count=1
    )
    page.goto(f"{seeded_server}/story-arcs/catalog/79")
    controls = page.get_by_test_id("story-arc-preview-table").locator(
        "[data-order-controls] button"
    )
    expect(controls).to_have_count(2)
    expect(controls.nth(0)).to_be_disabled()
    expect(controls.nth(1)).to_be_disabled()
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=("101", "102"), declared_issue_count=2
    )
    retry = page.get_by_role("button", name="Retry preview", exact=True)
    retry.click()
    expect(controls).to_have_count(4)
    expect(controls.nth(1)).to_be_enabled()
    held: list[Route] = []
    page.route("**/story-arcs/catalog/79", lambda route: held.append(route))
    with page.expect_request("**/story-arcs/catalog/79"):
        retry.click()
    expect(retry).to_be_disabled()
    for index in range(4):
        expect(controls.nth(index)).to_be_disabled()
    held.pop().continue_()
    expect(retry).to_be_enabled()
    expect(controls.nth(1)).to_be_enabled()


def test_preview_workspace_layout_and_accessibility(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.goto(f"{seeded_server}/story-arcs/catalog/78")
    workspace = page.get_by_test_id("story-arc-catalog-preview")
    expect(page.get_by_test_id("story-arc-preview-footer-dock")).to_be_visible()
    expect(page.locator("[data-provider-issue-id]")).to_have_count(2)
    add = page.get_by_role("button", name="Add Story Arc", exact=True).bounding_box()
    retry = page.get_by_role("button", name="Retry preview").bounding_box()
    cancel = page.get_by_role("link", name="Cancel", exact=True).bounding_box()
    assert add and retry and cancel
    assert abs(add["y"] - retry["y"]) < 2 and abs(cancel["y"] - retry["y"]) < 2
    assert add["x"] < retry["x"] < cancel["x"]
    output = Path("test-results/story-arc-preview")
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        toggle = page.get_by_test_id("header-theme-toggle")
        target = f"Switch to {theme} mode"
        if toggle.get_attribute("aria-label") != target:
            toggle.click()
        page.get_by_role("button", name=target, exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        assert_no_axe_violations(
            page, name=f"arc-preview-{theme}", include=["#content", "#page-footer-dock"]
        )
        page.screenshot(path=str(output / f"{theme}.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    assert page.locator("#content").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    expect(workspace.get_by_role("button", name="Retry preview")).to_be_visible()
    assert_no_axe_violations(
        page, name="arc-preview-mobile", include=["#content", "#page-footer-dock"]
    )
    page.screenshot(path=str(output / "mobile.png"), full_page=True)
