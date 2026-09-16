"""Rescan progress and saved results stay on the series page without navigation."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_rescan_progress_completes_without_replacing_the_page(authed_page, seeded_server, tmp_path):
    page = authed_page
    state = {"started": False, "complete": False}

    def report(route):
        if route.request.method == "POST":
            state["started"] = True
            route.fulfill(status=202, json={"job_id": "rescan-test", "state": "QUEUED"})
            return
        job = None
        if state["started"]:
            job = {
                "id": "rescan-test",
                "state": "COMPLETED" if state["complete"] else "RUNNING",
                "active": not state["complete"],
                "percent": 100 if state["complete"] else 25,
                "error": None,
            }
        route.fulfill(
            json={
                "job": job,
                "counts": {"added": 1, "review": 1} if state["complete"] else {},
                "items": [
                    {
                        "id": "file-1",
                        "path": "/comics/Batman/mystery.cbz",
                        "reason": "No unique issue matches.",
                    }
                ]
                if state["complete"]
                else [],
                "page": 1,
                "pages": 1,
            }
        )

    page.route("**/api/v1/series/1/rescan**", report)
    page.goto(f"{seeded_server}/series/1")
    page.evaluate(
        "window.rescanPageSentinel = document.querySelector('[data-testid=series-detail-page]')"
    )
    page.get_by_test_id("series-action-rescan").click()
    dialog = page.get_by_role("dialog", name="Rescan folder")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "25")
    expect(page.get_by_test_id("series-action-rescan")).to_be_disabled()
    state["complete"] = True
    expect(dialog.get_by_text("Rescan complete", exact=True)).to_be_visible(timeout=10000)
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "100")
    expect(dialog.get_by_text("1 added", exact=False)).to_be_visible()
    expect(dialog.get_by_role("button", name="Review in Import")).to_be_visible()
    expect(page.get_by_test_id("series-action-rescan")).to_be_enabled()
    assert page.evaluate(
        "window.rescanPageSentinel === document.querySelector('[data-testid=series-detail-page]')"
    )
    page.screenshot(path=str(tmp_path / "series-rescan-complete.png"))
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_test_id("series-action-rescan")).to_be_focused()
    page.get_by_role("button", name="Rescan results", exact=True).click()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    requests = []

    def start_import(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=201, json={"id": 17})

    page.route("**/api/v1/import", start_import)
    page.route(
        "**/import?tab=collection&resume_job_id=17",
        lambda route: route.fulfill(content_type="text/html", body="Import review"),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Rescan results", exact=True).click()
    bounds = dialog.bounding_box()
    assert bounds is not None and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "series-rescan-mobile.png"))
    dialog.get_by_role("button", name="Review in Import").click()
    expect(page).to_have_url(f"{seeded_server}/import?tab=collection&resume_job_id=17")
    assert requests == [
        {
            "source_path": "/comics/Batman/mystery.cbz",
            "file_paths": ["/comics/Batman/mystery.cbz"],
            "source_type": "filesystem",
            "file_handling_mode": "in_place",
        }
    ]
