"""Mass Convert describes its active metadata writer without changing controls."""

import pytest
from playwright.sync_api import expect

from pullbox.config import get_settings
from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_mass_paired_metadata_copy_and_keyboard_toggle(
    authed_page, seeded_server, monkeypatch, theme, width
):
    settings = get_settings().model_copy(update={"metadata_paired_conversion_writer_enabled": True})
    monkeypatch.setattr("pullbox.ui.utilities_routes.get_settings", lambda: settings)
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/utilities/mass-convert")
    page.evaluate("theme => applyTheme(theme)", theme)

    expect(page.get_by_text("Embed paired metadata", exact=True)).to_be_visible()
    help_text = page.locator("#utilities-mass-convert-metadata-help")
    expect(help_text).to_contain_text("ComicInfo.xml and MetronInfo.xml")
    expect(help_text).to_be_visible()
    checkbox = page.locator('input[x-model="steps.metadata"]')
    expect(checkbox).to_be_checked()
    checkbox.focus()
    checkbox.press("Space")
    expect(checkbox).not_to_be_checked()
    expect(checkbox).to_be_focused()
    expect(help_text).not_to_be_visible()
    checkbox.press("Space")
    expect(help_text).to_be_visible()
    expect(checkbox).to_be_focused()
    expect(page.get_by_test_id("utilities-mass-convert-footer-dock")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert_no_axe_violations(
        page,
        name=f"mass-paired-metadata-{theme}",
        include=['[data-testid="utilities-mass-convert-workspace"]'],
    )
