"""Browser coverage for source-preserving storage choices when adding an arc."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("prefix", [False, True])
def test_new_arc_copy_policy_keeps_original_names_with_optional_prefix(
    authed_page: Page, seeded_server: str, prefix: bool
) -> None:
    page = authed_page
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(f"{seeded_server}/story-arcs", wait_until="domcontentloaded")
    page.get_by_text("Create Story Arc", exact=True).click()
    form = page.get_by_test_id("story-arcs-create-form")
    form.get_by_label("Name", exact=True).fill(f"Copy policy browser test {prefix}")
    root = form.get_by_label("Library root", exact=True)
    expect(root).to_be_hidden()
    form.get_by_label("Separate folder").select_option("copy")
    expect(root).to_be_visible()
    expect(root).to_have_attribute("required", "required")
    option = root.locator("option").nth(1)
    root_value = option.get_attribute("value")
    assert root_value is not None
    root_path = option.inner_text().split(" — ", 1)[1]
    root.select_option(root_value)
    form.get_by_label("Arc folder location").fill(root_path)
    expect(form.get_by_label("Issue filenames")).to_have_value("original")
    expect(form.get_by_label("Filename template", exact=True)).to_be_hidden()
    expect(form.get_by_label("Reading-order digits")).to_be_hidden()
    if prefix:
        form.get_by_label("Prefix arc filenames with the reading order").check()
        expect(form.get_by_label("Reading-order digits")).to_be_visible()
        expect(form.get_by_label("Reading-order digits")).to_have_value("2")
    assert_no_axe_violations(
        page,
        name=f"story-arc-create-copy-{prefix}",
        include=["[data-testid='story-arcs-create-form']"],
    )
    form.get_by_role("button", name="Create empty Story Arc").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=created$"))
    expect(page.get_by_test_id("story-arc-detail-page")).to_be_visible()
    expect(page.get_by_label("Issue file template")).to_have_value(
        "{ReadingOrder:02d} - {OriginalFilename}" if prefix else "{OriginalFilename}"
    )
    assert page_errors == []
