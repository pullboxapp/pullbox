"""Mocked-provider browser coverage for reviewed Story Arc adoption and refresh."""

from __future__ import annotations

import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.accessibility import assert_no_axe_violations
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
    root_path = root_option.inner_text().split(" — ", 1)[1]
    canonical_root.select_option(root_id)
    form.get_by_label("Separate folder").select_option("copy")
    form.get_by_label("Library root", exact=True).select_option(root_id)
    form.get_by_label("Arc folder location").fill(root_path)
    form.get_by_label("Prefix arc filenames with the reading order").check()
    expect(form.get_by_label("Reading-order digits")).to_have_value("2")
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
    expect(page.get_by_label("Include upcoming issues")).to_have_count(0)
    monitor = page.get_by_role("switch", name="Toggle monitoring for this Story Arc")
    expect(monitor).to_be_visible()
    # Boosted navigation updates the URL before HTMX's settle pass binds the
    # new form. A keyboard press must wait for that pass as well as visibility.
    expect(page.locator("body")).not_to_have_attribute("data-shell-pending", "")
    expect(page.locator("#content .htmx-added, #content.htmx-settling")).to_have_count(0)
    page.wait_for_load_state("load")
    assert errors == []
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
    expect(page.get_by_label("Issue file template")).to_have_value(
        "{ReadingOrder:02d} - {OriginalFilename}"
    )
    expect(page.get_by_role("link", name="Open issue 1AU")).to_have_attribute(
        "href", re.compile(r"/issues/\d+\?source=story-arc&story_arc_id=\d+")
    )
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=("101", "103")
    )
    page.get_by_role("link", name="Review provider changes").click()
    expect(page.get_by_text("Comic Vine issue ID 102 — preserved")).to_be_visible()
    page.get_by_label("I reviewed these provider changes").check()
    page.get_by_role("button", name="Save reviewed provider changes").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=catalog-refreshed$"))
    expect(page.locator("[data-membership-id]")).to_have_count(3)
    expect(page.locator("[data-membership-id]").nth(0)).to_have_attribute(
        "data-exact-issue-number", "1AU"
    )
    page.get_by_role("button", name="Review issue 2 match").click()
    expect(page.get_by_role("button", name="Confirm reading order")).to_be_visible()
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
