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
        assert route.request.post_data_json == {"fields": ["title"]}
        state.update(
            {
                "job": {"id": "a" * 32, "state": "COMPLETED", "percent": 100, "error": None},
                "mode": "preview",
                "fields": ["title"],
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
                                "document": "ComicInfo.xml",
                                "field": "ComicInfo/Series[1]",
                                "before": "Old",
                                "after": "New",
                            }
                        ],
                    }
                    for i in [1, 2]
                ],
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
    expect(dialog.get_by_role("button", name="Preview comic files")).to_be_disabled()
    dialog.get_by_role("checkbox", name="Series name", exact=True).check()
    dialog.get_by_role("button", name="Preview comic files").click()
    expect(dialog.get_by_role("status").filter(has_text="2 ready")).to_be_visible()
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
