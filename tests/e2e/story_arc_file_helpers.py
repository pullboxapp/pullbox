"""Exercise global arc defaults through the same settings controls users use."""

from playwright.sync_api import Page, expect


def configure_arc_file_defaults(page: Page, base_url: str, *, prefix: bool) -> None:
    page.goto(f"{base_url}/settings?tab=media#story-arc-files", wait_until="load")
    section = page.get_by_test_id("settings-story-arc-files")
    # Wait for Alpine, then use the visible label rather than its clipped input.
    expect(section.get_by_test_id("arc-files-preview")).to_contain_text("The Court of Owls/")
    enabled = section.get_by_label("Create separate story arc folders")
    if not enabled.is_checked():
        enabled.locator("..").click()
    expect(enabled).to_be_checked()
    root = section.get_by_test_id("arc-files-root")
    root_trigger = root.locator("[data-dropdown-select-trigger]")
    expect(root_trigger).to_be_visible()
    root_trigger.click()
    options = page.locator("[data-dropdown-select-panel]:visible [data-dropdown-option]")
    option = options.nth(1)
    root_path = option.inner_text().split(" — ", 1)[1].strip()
    option.click()
    section.get_by_label("Arc folder location").fill(root_path)
    prefix_toggle = section.get_by_label("Prefix arc filenames with the reading order")
    if prefix_toggle.is_checked() != prefix:
        prefix_toggle.locator("..").click()
    expect(prefix_toggle).to_be_checked(checked=prefix)
    expect(section.get_by_test_id("arc-files-preview")).to_contain_text(
        "The Court of Owls/01 - Batman 001.cbz" if prefix else "The Court of Owls/Batman 001.cbz"
    )
    save = section.get_by_role("button", name="Save story arc defaults")
    if save.is_enabled():
        with page.expect_response(
            lambda response: (
                response.url.endswith("/api/v1/config") and response.request.method == "PUT"
            )
        ) as saved:
            save.click()
        assert saved.value.status == 200, saved.value.text()
        expect(save).to_be_disabled()
    page.reload(wait_until="load")
    expect(section.get_by_label("Create separate story arc folders")).to_be_checked()
    expect(section.get_by_label("Prefix arc filenames with the reading order")).to_be_checked(
        checked=prefix
    )
