"""Scoped naming behaves consistently with real saves and delayed requests."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


def _open(page: Page, base_url: str) -> Locator:
    page.goto(f"{base_url}/settings?tab=media", wait_until="load")
    editor = page.get_by_test_id("settings-naming-editor")
    expect(editor.locator("#preview-standard")).to_contain_text("Absolute Batman")
    return editor


def _scope(page: Page, index: int) -> None:
    page.get_by_test_id("settings-naming-scope").locator("[data-dropdown-select-trigger]").click()
    page.locator("[data-dropdown-select-panel]:visible [data-dropdown-option]").nth(index).click()


def _settle_theme(page: Page) -> None:
    page.wait_for_function("""() => document.querySelector('[data-testid="settings-naming-editor"]')
        .getAnimations({subtree: true}).every(animation => animation.playState !== 'running')""")


def _save(page: Page, editor: Locator, *, global_scope: bool = False) -> None:
    name = "Save global defaults" if global_scope else "Save for this library"
    with page.expect_response(
        lambda response: (
            response.url.endswith("/config/naming") and response.request.method == "PUT"
        )
    ) as saved:
        editor.get_by_role("button", name=name, exact=True).click()
    assert saved.value.status == 200, saved.value.text()
    expect(editor.get_by_role("button", name=name, exact=True)).to_be_disabled()


def test_scoped_naming_inheritance_saves_and_unsaved_changes(
    authed_page: Page, seeded_server: str
) -> None:
    page = authed_page
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    editor = _open(page, seeded_server)
    folder = editor.get_by_label("Series Folder Format", exact=True)
    original = folder.input_value()
    inherit = editor.get_by_label("Use global naming defaults", exact=True)
    # Keyboard selection uses the same shared dropdown contract as other settings.
    trigger = page.get_by_test_id("settings-naming-scope").locator("[data-dropdown-select-trigger]")
    trigger.focus()
    trigger.press("Enter")
    expect(
        page.locator("[data-dropdown-select-panel]:visible [data-dropdown-option]").first
    ).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(inherit).to_be_checked()
    expect(folder).to_be_disabled()
    expect(inherit).to_be_enabled()
    inherit.press("Space")
    expect(folder).to_be_enabled()
    folder.fill("{Publisher}/{Series} ({Year})")
    _scope(page, 0)
    expect(page.get_by_role("heading", name="Discard naming changes?")).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(folder).to_have_value("{Publisher}/{Series} ({Year})")
    expect(editor.get_by_test_id("settings-naming-scope-status")).to_contain_text("this library")
    _scope(page, 0)
    page.get_by_role("button", name="Discard changes", exact=True).click()
    expect(folder).to_have_value(original)
    _scope(page, 1)
    expect(inherit).to_be_checked()
    expect(inherit).to_be_enabled()
    inherit.press("Space")
    folder.fill("{Publisher}/{Series} ({Year})")
    expect(editor.locator("#preview-folder")).to_contain_text("DC Comics/Absolute Batman")
    _save(page, editor)
    page.reload(wait_until="load")
    expect(editor.locator("#preview-standard")).to_contain_text("Absolute Batman")
    _scope(page, 1)
    expect(inherit).not_to_be_checked()
    expect(folder).to_have_value("{Publisher}/{Series} ({Year})")
    _scope(page, 0)
    expect(editor.get_by_test_id("settings-naming-scope-status")).to_have_text(
        "Editing global defaults"
    )
    folder.fill("{Series} [{Year}]")
    _save(page, editor, global_scope=True)
    _scope(page, 1)
    expect(folder).to_have_value("{Publisher}/{Series} ({Year})")
    expect(inherit).to_be_enabled()
    inherit.press("Space")
    expect(folder).to_be_disabled()
    expect(folder).to_have_value("{Series} [{Year}]")
    editor.get_by_role("button", name="Save for this library", exact=True).click()
    expect(page.get_by_role("heading", name="Use global naming defaults?")).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(editor.get_by_role("button", name="Save for this library", exact=True)).to_be_enabled()
    editor.get_by_role("button", name="Reset", exact=True).click()
    expect(inherit).not_to_be_checked()
    expect(folder).to_have_value("{Publisher}/{Series} ({Year})")
    expect(inherit).to_be_enabled()
    inherit.press("Space")
    expect(folder).to_have_value("{Series} [{Year}]")
    editor.get_by_role("button", name="Save for this library", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/config/naming") and response.request.method == "PUT"
        )
    ) as saved:
        page.get_by_role("button", name="Use global defaults", exact=True).click()
    assert saved.value.status == 200
    expect(editor.get_by_role("button", name="Save for this library", exact=True)).to_be_disabled()
    _scope(page, 0)
    expect(folder).to_be_enabled()
    folder.fill(original)
    _save(page, editor, global_scope=True)
    _scope(page, 1)
    expect(inherit).to_be_checked()
    expect(folder).to_have_value(original)
    assert not errors


def test_naming_errors_delayed_preview_and_accessibility(
    authed_page: Page, seeded_server: str
) -> None:
    page = authed_page
    editor = _open(page, seeded_server)
    field = editor.get_by_label("Standard Comic Format", exact=True)
    held: list[Route] = []

    def preview(route: Route) -> None:
        if "old-preview" in (route.request.post_data or ""):
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/config/naming/preview", preview)
    with page.expect_request("**/api/v1/config/naming/preview"):
        field.fill("{Series} old-preview")
    field.fill("{Series} latest-preview")
    expect(editor.locator("#preview-standard")).to_contain_text("latest-preview")
    assert len(held) == 1
    held[0].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {"examples": {"comic_file_template": [{"input": "stale", "output": "old-preview"}]}}
        ),
    )
    expect(editor.locator("#preview-standard")).not_to_contain_text("old-preview")
    page.route(
        "**/api/v1/config/naming",
        lambda route: (
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {"error": {"message": "Naming settings changed. Reload this scope."}}
                ),
            )
            if route.request.method == "PUT"
            else route.continue_()
        ),
    )
    editor.get_by_role("button", name="Save global defaults", exact=True).click()
    expect(editor.get_by_role("alert")).to_contain_text("Naming settings changed.")
    expect(field).to_have_value("{Series} latest-preview")
    expect(editor.get_by_role("button", name="Reset", exact=True)).to_be_visible()
    field.fill("{Unknown}")
    expect(editor.get_by_role("button", name="Save global defaults", exact=True)).to_be_disabled()
    editor.get_by_role("button", name="Reset", exact=True).click()
    expect(editor.locator("#preview-standard")).to_contain_text("Absolute Batman")
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        _settle_theme(page)
        assert_no_axe_violations(
            page, name=f"naming-{theme}", include=["[data-testid='settings-naming-editor']"]
        )
    page.set_viewport_size({"width": 320, "height": 900})
    editor.get_by_label("Series Folder Format", exact=True).scroll_into_view_if_needed()
    assert editor.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    _settle_theme(page)
    assert_no_axe_violations(
        page, name="naming-narrow", include=["[data-testid='settings-naming-editor']"]
    )
