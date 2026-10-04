"""Series metadata output stays explicit and uses the shared modal contracts."""

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_series_sidecar_preview_write_and_focus(authed_page, seeded_server, theme, width):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    endpoint = "**/api/v1/series/1/sidecar"
    data = {
        "series_id": 1,
        "review_key": "a" * 64,
        "ready": True,
        "aliases": ["Alternate Batman"],
        "snapshot": {
            "values": {"title": "Batman", "publisher": "DC", "year_start": 1940, "issue_count": 3},
            "identities": [{"namespace": "comicvine", "external_id": "796"}],
            "origins": [
                {
                    "field": "publisher",
                    "source": None,
                    "passive_release": {
                        "locg_series_id": "77",
                        "release_ids": ["1001", "1002"],
                        "fetched_at": "2026-10-04T00:00:00Z",
                    },
                }
            ],
        },
        "targets": [
            {"directory": "/comics/Batman", "action": "create", "reason": None},
            {
                "directory": "/legacy/Batman",
                "action": "blocked",
                "reason": "Files kept in place are not modified.",
            },
        ],
    }
    page.route(endpoint + "/preview", lambda route: route.fulfill(json=data))
    calls = []

    def write(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json={"written": 1, "unchanged": 0, "targets": data["targets"]})

    page.route(endpoint + "/write", write)
    page.goto(f"{seeded_server}/series/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_role("button", name="Write series metadata", exact=True)
    expect(trigger).to_be_visible()
    trigger.click()
    dialog = page.get_by_role("dialog", name="Write series metadata", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("/comics/Batman", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Files kept in place are not modified.", exact=True)).to_be_visible()
    dialog.get_by_text("Compiled metadata", exact=True).click()
    compiled = dialog.locator("pre")
    expect(compiled).to_contain_text('"passive_release"')
    expect(compiled).to_contain_text('"locg_series_id": "77"')
    expect(compiled).to_contain_text('"1001"')
    expect(compiled).to_contain_text('"1002"')
    compiled.focus()
    expect(dialog.get_by_role("region", name="Compiled series metadata")).to_be_focused()
    compiled.press("End")
    page.wait_for_function(
        "() => document.querySelector('[aria-label=\"Compiled series metadata\"]').scrollTop > 0"
    )
    assert calls == [], "Preview must never write files"
    assert_no_axe_violations(
        page, name=f"series-sidecar-{theme}", include=['[aria-labelledby="series-sidecar-title"]']
    )
    dialog.get_by_role("button", name="Write series.json", exact=True).click()
    expect(dialog.get_by_role("status")).to_contain_text("Wrote 1 series.json file")
    assert calls == [{"review_key": "a" * 64}]
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()


def test_no_safe_location_disables_sidecar_write(authed_page, seeded_server):
    page = authed_page
    page.route(
        "**/api/v1/series/1/sidecar/preview",
        lambda route: route.fulfill(
            json={
                "series_id": 1,
                "review_key": "a" * 64,
                "ready": False,
                "aliases": [],
                "snapshot": {"values": {"title": "Batman"}, "identities": []},
                "targets": [
                    {
                        "directory": "/legacy/Batman",
                        "action": "blocked",
                        "reason": "Files kept in place are not modified.",
                    }
                ],
            }
        ),
    )
    page.goto(f"{seeded_server}/series/1")
    page.get_by_role("button", name="Write series metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Write series metadata", exact=True)
    expect(dialog.get_by_role("button", name="Write series.json", exact=True)).to_be_disabled()
    expect(dialog.get_by_text("Files kept in place are not modified.", exact=True)).to_be_visible()
