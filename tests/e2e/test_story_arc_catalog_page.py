"""Mocked-provider browser coverage for reviewed Story Arc adoption and refresh."""

from __future__ import annotations

import re
from dataclasses import replace
from unittest.mock import AsyncMock

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
    expect(page.get_by_role("link", name="Preview Numbering Event")).to_be_visible()
    query.press("Tab")
    page.get_by_role("link", name="Preview Numbering Event").focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/story-arcs/catalog/42")
    expect(
        page.get_by_text("Comic Vine response order — reading order unverified.", exact=False)
    ).to_be_visible()
    form = page.get_by_test_id("story-arc-catalog-add-form")
    form.get_by_label("Order for #1000000", exact=True).fill("2")
    form.get_by_label("Order for #1AU", exact=True).fill("1")
    canonical_root = form.get_by_label("Library root for new series")
    root_option = canonical_root.locator("option").nth(1)
    root_id = root_option.get_attribute("value")
    assert root_id is not None
    canonical_root.select_option(root_id)
    expect(form.get_by_label("Separate folder")).to_have_count(0)
    expect(form.get_by_test_id("story-arc-create-storage")).to_contain_text(
        "Copy issues into arc folders"
    )
    form.get_by_role("button", name="Add reviewed Story Arc").click()
    expect(page).to_have_url(re.compile(r"/story-arcs/catalog/42$"))
    assert form.get_by_label("I reviewed the reading order").evaluate(
        "element => !element.checkValidity()"
    )
    form.get_by_label("I reviewed the reading order").check()
    assert_no_axe_violations(
        page, name="story-arc-catalog-review", include=["[data-testid='story-arc-catalog-preview']"]
    )
    form.get_by_role("button", name="Add reviewed Story Arc").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=catalog-added$"))
    arc_url = page.url.split("?", 1)[0]
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
    expect(page.get_by_role("link", name="Already added — open arc")).to_have_attribute(
        "href", arc_url.removeprefix(seeded_server)
    )
    assert errors == []


def test_catalog_partial_failure_and_retry_are_visible(
    authed_page: Page, seeded_server: str, catalog_provider: CatalogProvider
) -> None:
    page = authed_page
    catalog_provider.metadata = replace(
        catalog_provider.metadata, membership_complete=False, declared_issue_count=3
    )
    page.goto(f"{seeded_server}/story-arcs/catalog/99")
    expect(page.get_by_role("alert")).to_contain_text("Incomplete member list")
    expect(page.get_by_test_id("story-arc-catalog-add-form")).to_have_count(0)
    catalog_provider.fail = True
    page.get_by_role("link", name="Retry preview").click()
    expect(page.get_by_role("alert")).to_contain_text("couldn't load this arc")
    catalog_provider.fail = False
    catalog_provider.metadata = replace(
        catalog_provider.metadata, membership_complete=True, declared_issue_count=2
    )
    page.get_by_role("link", name="Retry preview").click()
    expect(page.get_by_test_id("story-arc-catalog-add-form")).to_be_visible()
    page.set_viewport_size({"width": 640, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    assert_no_axe_violations(
        page, name="story-arc-catalog-narrow", include=["[data-testid='story-arc-catalog-preview']"]
    )
