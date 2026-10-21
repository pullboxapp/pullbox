"""Manual import offers choices before starting background file work."""

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def enable_manual_metadata(seeded_server, monkeypatch):
    from pullbox.config import get_settings
    from pullbox.database import get_session_factory
    from pullbox.models.config import SystemConfig

    # A second in-process server would replace the shared database engine.
    settings = get_settings().model_copy(update={"metadata_paired_import_writer_enabled": True})
    monkeypatch.setattr("pullbox.ui.series_detail_routes.get_settings", lambda: settings)

    async def configure(value):
        async with get_session_factory().begin() as session:
            key = "update_embedded_comicinfo_from_match_on_import"
            row = await session.get(SystemConfig, key)
            previous = row.value if row is not None else None
            if value is None:
                if row is not None:
                    await session.delete(row)
            elif row is not None:
                row.value = value
            else:
                session.add(SystemConfig(key=key, value=value, value_type="bool"))
            return previous

    previous = _run_async_blocking(configure("true"))
    try:
        yield
    finally:
        _run_async_blocking(configure(previous))


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("dark", 1280)])
def test_manual_import_reviews_fields_before_start(
    authed_page, seeded_server, theme, width, tmp_path
):
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
    page.screenshot(path=tmp_path / f"bug8-review-{theme}-{width}.png")
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


@pytest.mark.parametrize(
    "theme,width,height", [("light", 1280, 720), ("dark", 390, 844), ("light", 844, 390)]
)
def test_long_manual_review_scrolls_between_visible_header_and_actions(
    authed_page, seeded_server, theme, width, height, tmp_path
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": height})
    conflicts = [
        {
            "key": "issue.cover_date",
            "label": "Issue cover date",
            "options": [
                {"source": "library", "value": "2025-02-01", "selectable": True},
                {"source": "ComicInfo.xml", "value": "2024-12-?", "selectable": False},
            ],
        },
        {
            "key": "issue.page_count",
            "label": "Issue page count",
            "options": [
                {"source": "library", "value": "48", "selectable": True},
                {"source": "ComicInfo.xml", "value": "54", "selectable": True},
            ],
        },
        {
            "key": "issue.credits",
            "label": "Issue credits",
            "options": [
                {
                    "source": "library",
                    "value": "\n".join(f"Catalog creator {index} (artist)" for index in range(13)),
                    "selectable": True,
                },
                {"source": "ComicInfo.xml", "value": "Rafael Grampa (writer)", "selectable": True},
            ],
        },
    ]
    page.route(
        "**/api/v1/issues/2/import-file/metadata-preview",
        lambda route: route.fulfill(
            json={
                "ready": False,
                "review_key": "a" * 64,
                "context_key": "b" * 64,
                "conflicts": conflicts,
            }
        ),
    )
    page.goto(f"{seeded_server}/issues/2")
    page.evaluate("theme => applyTheme(theme)", theme)
    shell_scroll = page.locator("#content").evaluate("el => el.scrollTop")
    page.evaluate("""() => Alpine.$data(document.querySelector('[data-testid="issue-detail-page"]'))
        .applyImportFileSelection({path:'/imports/gargoyle-003.cbz', name:'gargoyle-003.cbz', size:183465435})""")
    modal = page.get_by_test_id("issue-import-modal")
    expect(modal.get_by_text("Issue credits", exact=True)).to_be_visible()
    panel = modal.get_by_role("dialog")
    expect(panel).to_be_in_viewport(ratio=1)
    header = panel.locator(":scope > .modal-header")
    footer = panel.locator(":scope > .modal-footer")
    body = panel.locator(":scope > .modal-body")
    expect(header).to_be_in_viewport(ratio=1)
    expect(footer).to_be_in_viewport(ratio=1)
    assert body.evaluate("el => el.scrollHeight > el.clientHeight && el.clientHeight > 40")
    header_before, footer_before = header.bounding_box(), footer.bounding_box()
    modal.get_by_role("radio", name="library: 2025-02-01", exact=True).check()
    modal.get_by_role("radio", name="ComicInfo.xml: 54", exact=True).check()
    modal.get_by_role("radio", name="library: Catalog creator 0", exact=False).check()
    assert body.evaluate("el => el.scrollTop > 0")
    assert header.bounding_box() == header_before
    assert footer.bounding_box() == footer_before
    assert page.locator("#content").evaluate("el => el.scrollTop") == shell_scroll
    button = footer.get_by_role("button", name="Import reviewed file", exact=True)
    expect(button).to_be_enabled()
    expect(button).to_be_in_viewport(ratio=1)
    page.screenshot(path=tmp_path / f"bug8-scroll-{theme}-{width}x{height}.png")
    footer.get_by_role("button", name="Close", exact=True).click()
    expect(modal).not_to_be_visible()
