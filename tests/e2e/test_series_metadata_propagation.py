"""Selected-field propagation uses the shared scrollable modal and progress contracts."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("tron", 390)])
def test_preview_selection_progress_and_modal_bounds(authed_page, live_server, theme, width):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 800})
    state = {"job": None}
    writes = []
    page.route("**/api/v1/series/1/metadata-propagation/job*", lambda r: r.fulfill(json=state))

    def preview(route):
        assert route.request.post_data_json == {"fields": ["publication_titles"]}
        state.update(
            {
                "job": {"id": "a" * 32, "state": "COMPLETED", "percent": 100, "error": None},
                "mode": "preview",
                "fields": ["publication_titles"],
                "counts": {"ready": 2, "attention": 1},
                "ready_ids": [1, 2],
                "page": 1,
                "pages": 1,
                "retryable": False,
                "targets": [
                    {
                        "issue_id": i,
                        "label": f"Issue {i}",
                        "file_name": f"example-{i}.cbz",
                        "outcome": "ready",
                        "reason": "",
                        "changes": [
                            {
                                "document": "MetronInfo.xml",
                                "field": "MetronInfo/Series[1]/AlternativeNames[1]/AlternativeName[1]",
                                "before": "Old",
                                "after": "New",
                            }
                        ],
                    }
                    for i in [1, 2]
                ],
            }
        )
        state["targets"].append(
            {
                "issue_id": 3,
                "label": "Issue 3",
                "outcome": "attention",
                "file_name": "example-3.cbz",
                "reason": "This change would remove an embedded title identifier.",
                "protected_titles": {
                    "entries": [{"name": "Port <i>Nord</i>", "language": "fr"}],
                    "not_shown": 0,
                },
            }
        )
        route.fulfill(status=202, json={"job_id": "a" * 32, "state": "QUEUED"})

    def write(route):
        writes.append(route.request.post_data_json)
        state.update(
            {
                "mode": "write",
                "job": {"id": "b" * 32, "state": "RUNNING", "percent": 42, "error": None},
                "ready_ids": [],
                "targets": [],
                "counts": {"running": 1},
            }
        )
        route.fulfill(status=202, json={"job_id": "b" * 32, "state": "RUNNING"})

    page.route("**/api/v1/series/1/metadata-propagation/preview", preview)
    page.route("**/api/v1/series/1/metadata-propagation/write", write)
    page.goto(live_server + "/series/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    page.get_by_role("button", name="Update comic metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Update series comic metadata")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("checkbox", name="Total volumes", exact=True)).not_to_be_checked()
    expect(dialog.get_by_role("button", name="Preview comic files")).to_be_disabled()
    titles = dialog.get_by_role("checkbox", name="Alternate publication titles", exact=True)
    expect(titles).not_to_be_checked()
    titles.check()
    dialog.get_by_role("button", name="Preview comic files").click()
    expect(dialog.get_by_role("status").filter(has_text="2 ready")).to_be_visible()
    blocked = dialog.get_by_role("article").filter(has_text="Issue 3")
    protected = blocked.get_by_role("list", name="Titles to keep")
    expect(protected.get_by_text("Port <i>Nord</i> (fr)", exact=True)).to_be_visible()
    expect(protected.locator("i")).to_have_count(0)
    expect(blocked.get_by_role("checkbox", name="Update Issue 3")).not_to_be_visible()
    summary = blocked.get_by_text("Protected titles and languages", exact=True)
    summary.focus()
    page.keyboard.press("Enter")
    expect(protected).not_to_be_visible()
    page.keyboard.press("Enter")
    expect(protected).to_be_visible()
    expect(blocked.get_by_text("In Edit metadata, open All fields", exact=False)).to_be_visible()
    assert_no_axe_violations(
        page,
        name=f"protected-titles-{theme}",
        include=["[data-testid='series-propagation-dialog']"],
    )
    dialog.get_by_role("checkbox", name="Update Issue 2").uncheck()
    dialog.get_by_role("button", name="Update 1 selected file", exact=True).click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "42")
    assert writes == [{"job_id": "a" * 32, "issue_ids": [1]}]
    assert page.evaluate("""() => { const d=document.querySelector('[data-testid="series-propagation-dialog"]');
      const r=d.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth; }""")
    Path("test-results").mkdir(exist_ok=True)
    page.screenshot(path=f"test-results/series-propagation-{theme}-{width}.png")
    assert_no_axe_violations(
        page, name=f"propagation-{theme}", include=["[data-testid='series-propagation-dialog']"]
    )
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="Update comic metadata", exact=True)).to_be_focused()


def test_protected_title_opens_series_editor_without_writing_or_reusing_preview(
    authed_page, live_server
):
    page = authed_page
    state = {
        "job": {"id": "a" * 32, "state": "COMPLETED", "percent": 100},
        "mode": "preview",
        "fields": ["publication_titles"],
        "counts": {"ready": 1, "attention": 1},
        "ready_ids": [1],
        "targets": [
            {
                "issue_id": 3,
                "label": "Issue 3",
                "outcome": "attention",
                "protected_titles": {
                    "entries": [{"name": "Nord", "language": "fr"}],
                    "not_shown": 0,
                },
            }
        ],
    }
    page.route("**/api/v1/series/1/metadata-propagation/job*", lambda r: r.fulfill(json=state))
    writes = []
    page.on(
        "request",
        lambda r: writes.append(r.url) if r.method == "POST" and r.url.endswith("/write") else None,
    )
    page.goto(live_server + "/series/1")
    opener = page.get_by_role("button", name="Update comic metadata", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Update series comic metadata")
    blocked = dialog.get_by_role("article").filter(has_text="Issue 3")
    blocked.get_by_role("button", name="Edit series metadata", exact=True).click(timeout=3500)
    editor = page.get_by_role("dialog", name="Edit series metadata", exact=True)
    expect(dialog).not_to_be_visible()
    expect(editor).to_be_visible()
    expect(
        page.locator("#pb-confirm-dialog").get_by_role("button", name="Confirm")
    ).not_to_be_visible()
    editor.get_by_role("button", name="All fields").click()
    expect(
        editor.get_by_role("checkbox", name="Update Alternate publication titles")
    ).to_be_visible()
    editor.get_by_role("button", name="Close", exact=True).click()
    expect(page.get_by_role("button", name="Edit metadata", exact=True)).to_be_focused()
    opener.click()
    expect(dialog.get_by_role("button", name="Preview comic files")).to_be_enabled()
    expect(dialog.get_by_role("button", name="Update 1 selected file")).not_to_be_visible()
    assert not writes
