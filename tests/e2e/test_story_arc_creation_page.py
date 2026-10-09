"""Browser coverage for source-preserving storage choices when adding an arc."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.story_arc_file_helpers import configure_arc_file_defaults

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("delayed_endpoint", ["library-roots", "naming/preview"])
def test_arc_file_defaults_wait_for_media_loads_and_persist_prefix(
    authed_page: Page, seeded_server: str, delayed_endpoint: str
) -> None:
    # Late media data expands cards above the arc form. Record premature
    # pointer actions deterministically instead of relying on a mid-click race.
    premature_actions: list[str] = []
    authed_page.expose_function(
        "recordPrematureArcAction", lambda value: premature_actions.append(value)
    )
    authed_page.add_init_script(
        """(() => {
        const originalFetch = window.fetch;
        window.arcMediaPending = true;
        window.fetch = async function(input, options) {
            if (String(input).endsWith('/api/v1/config/DELAYED_ENDPOINT')) {
                window.arcMediaPending = true;
                try {
                    const [response] = await Promise.all([
                        originalFetch.call(this, input, options),
                        new Promise(resolve => setTimeout(resolve, 1200)),
                    ]);
                    return response;
                } finally {
                    window.arcMediaPending = false;
                }
            }
            return originalFetch.call(this, input, options);
        };
        document.addEventListener('pointerdown', event => {
            if (window.arcMediaPending && event.target.closest('#story-arc-files')) {
                window.recordPrematureArcAction(event.target.outerHTML);
            }
        }, true);
    })()""".replace("DELAYED_ENDPOINT", delayed_endpoint)
    )
    authed_page.goto(f"{seeded_server}/settings?tab=media", wait_until="load")
    response = authed_page.request.put(
        f"{seeded_server}/api/v1/config",
        headers={"X-CSRF-Token": authed_page.evaluate("readCsrfTokenFromBody()")},
        data={
            "values": {
                "story_arc_files_enabled": "false",
                "story_arc_files_library_root_id": "",
                "story_arc_files_destination": "",
                "story_arc_files_prefix_reading_order": "false",
            }
        },
    )
    assert response.status == 200, response.text()
    authed_page.reload(wait_until="load")
    configure_arc_file_defaults(authed_page, seeded_server, prefix=True)
    configure_arc_file_defaults(authed_page, seeded_server, prefix=False)
    assert premature_actions == []


@pytest.mark.parametrize("prefix", [False, True])
def test_new_arc_copy_policy_keeps_original_names_with_optional_prefix(
    authed_page: Page, seeded_server: str, prefix: bool
) -> None:
    page = authed_page
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    configure_arc_file_defaults(page, seeded_server, prefix=prefix)
    assert_no_axe_violations(
        page,
        name=f"story-arc-file-defaults-{prefix}",
        include=["[data-testid='settings-story-arc-files']"],
    )
    page.goto(f"{seeded_server}/story-arcs/add", wait_until="domcontentloaded")
    page.get_by_text("Create Story Arc", exact=True).click()
    form = page.get_by_test_id("story-arcs-create-form")
    form.get_by_label("Name", exact=True).fill(f"Copy policy browser test {prefix}")
    expect(form.get_by_label("Separate folder")).to_have_count(0)
    expect(form.get_by_test_id("story-arc-create-storage")).to_contain_text(
        "Copy issues into arc folders"
    )
    expect(form.get_by_test_id("story-arc-create-storage")).to_contain_text(
        "The Court of Owls/01 - Batman 001.cbz" if prefix else "The Court of Owls/Batman 001.cbz"
    )
    assert_no_axe_violations(
        page,
        name=f"story-arc-create-copy-{prefix}",
        include=["[data-testid='story-arcs-create-form']"],
    )
    form.get_by_role("button", name="Create empty Story Arc").click()
    page.wait_for_url(re.compile(r"/story-arcs/\d+\?notice=created$"))
    expect(page.get_by_test_id("story-arc-detail-page")).to_be_visible()
    expect(page.get_by_test_id("story-arc-placement-policy-form")).to_have_count(0)
    expect(page.get_by_test_id("story-arc-arc-files-summary")).to_contain_text(
        "Copied to arc folder"
    )
    assert page_errors == []
