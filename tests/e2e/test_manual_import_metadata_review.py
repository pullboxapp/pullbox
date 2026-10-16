"""Manual import offers choices before starting background file work."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking, _running_live_server

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def live_server():
    with (
        patch.dict(os.environ, {"PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED": "true"}),
        _running_live_server() as server,
    ):
        yield server


@pytest.fixture(autouse=True)
def enable_manual_metadata(seeded_server):
    from pullbox.database import get_session_factory
    from pullbox.models.config import SystemConfig

    async def configure():
        async with get_session_factory().begin() as session:
            await session.merge(
                SystemConfig(
                    key="update_embedded_comicinfo_from_match_on_import",
                    value="true",
                    value_type="bool",
                )
            )

    _run_async_blocking(configure())


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("dark", 1280)])
def test_manual_import_reviews_fields_before_start(authed_page, seeded_server, theme, width):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    starts = []
    endpoint = "**/api/v1/issues/2/import-file"

    def preview(route):
        selected = route.request.post_data_json.get("choices", {}).get("issue.page_count")
        route.fulfill(
            json={
                "ready": bool(selected),
                "review_key": ("b" if selected else "a") * 64,
                "context_key": "c" * 64,
                "conflicts": [
                    {
                        "key": "issue.page_count",
                        "label": "Issue page count",
                        "selected": selected,
                        "note": "Archive page counts can include covers and extras.",
                        "options": [
                            {"source": "library", "value": "48", "selectable": True},
                            {"source": "ComicInfo.xml", "value": "54", "selectable": True},
                        ],
                    }
                ],
            }
        )

    def start(route):
        starts.append(route.request.post_data_json)
        route.fulfill(status=202, json={"issue_id": 2, "state": "running", "message": "Importing"})

    page.route(endpoint + "/metadata-preview", preview)
    page.route(endpoint + "/start", start)
    page.route(
        endpoint + "/progress",
        lambda route: route.fulfill(
            json={
                "issue_id": 2,
                "state": "running",
                "current_file_progress_pct": 40,
            }
        ),
    )
    page.goto(f"{seeded_server}/issues/2")
    page.evaluate("theme => applyTheme(theme)", theme)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    page.evaluate("""() => Alpine.$data(document.querySelector('[data-testid="issue-detail-page"]'))
        .applyImportFileSelection({path:'/imports/fixture.cbz', name:'fixture.cbz', size:100})""")
    dialog = page.get_by_test_id("issue-import-modal")
    expect(dialog.get_by_text("Issue page count", exact=True)).to_be_visible()
    assert not starts
    button = dialog.get_by_role("button", name="Import reviewed file", exact=True)
    expect(button).to_be_disabled()
    dialog.get_by_role("radio", name="ComicInfo.xml: 54", exact=True).check()
    expect(button).to_be_enabled()
    button.focus()
    page.keyboard.press("Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    assert_no_axe_violations(
        page, name=f"manual-import-review-{theme}", include=['[data-testid="issue-import-modal"]']
    )
    page.screenshot(path=str(Path("/private/tmp") / f"bug8-review-{theme}-{width}.png"))
    button.click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "40")
    assert starts == [
        {
            "file_path": "/imports/fixture.cbz",
            "move_to_library": True,
            "allow_resource_safety_exception": False,
            "metadata_approval": {
                "review_key": "b" * 64,
                "choices": {"issue.page_count": "ComicInfo.xml"},
            },
        }
    ]
