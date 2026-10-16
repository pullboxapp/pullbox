"""Discovery is an inline explicit choice, with the normal verified Add preview."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect

from pullbox.config import get_settings
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_source_series_add import preview
from tests.e2e.test_whats_new_page import _seed_whats_new_current_week

pytestmark = pytest.mark.e2e


@pytest.fixture
def release_page(authed_page, seeded_server, monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    _run_async_blocking(_seed_whats_new_current_week())
    yield authed_page
    get_settings.cache_clear()


def install_responses(page):
    selection = {"cache_id": 1, "release_id": 260001, "fingerprint": "a" * 64}
    page.route(
        "**/api/v1/whats-new/resolve/**",
        lambda route: route.fulfill(
            json={
                "selection": selection,
                "title": "Atlas Deluxe",
                "publisher": "Zenith Press",
                "year": 2026,
                "can_link": True,
                "locg_series_id": "960001",
                "roots": [{"id": 1, "path": "/comics", "name": "Test"}],
            }
        ),
    )
    page.route(
        "**/whats-new/find-series/**",
        lambda route: route.fulfill(
            json={
                "search_results": [
                    {
                        "source": "metron_api",
                        "external_id": "42",
                        "title": "Verified series",
                        "year_start": 2024,
                        "publisher_name": "Test publisher",
                        "source_label": "Metron",
                        "issue_count": 12,
                        "cover_url": "",
                        "already_added": False,
                    }
                ],
                "search_source_messages": [],
                "search_error": None,
                "search_page": 1,
                "search_total_pages": 1,
                "search_total_results": 1,
                "search_source_options": [["all", "All enabled sources"], ["metron_api", "Metron"]],
            }
        ),
    )
    page.route("**/api/v1/metadata/series/preview", lambda route: route.fulfill(json=preview()))
    return selection


def test_explicit_inline_search_preview_add_preserves_release_table(release_page, seeded_server):
    page = release_page
    selection = install_responses(page)
    adds = []

    def add(route):
        adds.append(route.request.post_data_json)
        assert route.request.headers.get("x-csrf-token")
        route.fulfill(json={"id": 91, "title": "Verified series", "monitored": True})

    page.route("**/api/v1/series", add)
    page.goto(f"{seeded_server}/whats-new")
    table = page.get_by_test_id("whats-new-current-release-table")
    table.evaluate("el => { el.dataset.checkpoint = 'unchanged'; }")
    trigger = page.get_by_test_id("whats-new-find-add").first
    trigger.click()
    picker = page.get_by_test_id("whats-new-find-dialog")
    expect(picker).to_be_visible()
    expect(picker.get_by_label("Series title")).to_have_value("Atlas Deluxe")
    expect(picker.get_by_role("button", name="Select", exact=True)).to_be_visible()
    assert not adds
    picker.get_by_role("button", name="Select", exact=True).click()
    preview_dialog = page.get_by_test_id("add-series-dialog")
    expect(preview_dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
    preview_dialog.get_by_role("button", name="Add series", exact=True).click()
    expect(preview_dialog).not_to_be_visible()
    assert adds == [
        {
            "source": "metron_api",
            "external_id": "42",
            "source_revision": 7,
            "library_root_id": 1,
            "whats_new_selection": selection,
        }
    ]
    expect(table).to_have_attribute("data-checkpoint", "unchanged")
    expect(page.get_by_test_id("whats-new-local-series")).to_have_attribute("href", "/series/91")
    assert page.url == f"{seeded_server}/whats-new"


def test_sidebar_navigation_loads_inline_discovery_controller(release_page, seeded_server):
    page = release_page
    install_responses(page)
    page.goto(f"{seeded_server}/")
    page.get_by_role("link", name="What's New", exact=True).click()
    page.get_by_test_id("whats-new-find-add").first.click()
    picker = page.get_by_test_id("whats-new-find-dialog")
    expect(picker).to_be_visible()
    expect(picker.get_by_label("Series title")).to_have_value("Atlas Deluxe")
    expect(picker.get_by_role("button", name="Select", exact=True)).to_be_visible()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("monitor", 320)])
def test_find_dialog_focus_reflow_and_accessibility(
    release_page, seeded_server, theme, width, browser_name
):
    page = release_page
    install_responses(page)
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_test_id("whats-new-find-add").first
    trigger.click()
    dialog = page.get_by_test_id("whats-new-find-dialog")
    expect(dialog.get_by_label("Series title")).to_be_focused()
    expect(dialog.get_by_role("button", name="Select", exact=True)).to_be_visible()
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert_no_axe_violations(
        page, name=f"find-add-{theme}-{width}", include=["[data-testid='whats-new-find-dialog']"]
    )
    dialog.screenshot(
        path=f"output/playwright/find-add-{browser_name}-{theme}-{width}.png", animations="disabled"
    )
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()


def test_real_local_find_preview_add_link_reload_and_paused_status(
    release_page, seeded_server, monkeypatch, tmp_path, request
):
    from pullbox.api.v1 import series as series_api
    from pullbox.providers.metadata import sources
    from pullbox.services.catalog import reader as catalog_reader
    from pullbox.services.provider_artwork import ProviderArtworkClient
    from tests.unit.test_catalog_reader import installed_reader

    reader = installed_reader(tmp_path)
    monkeypatch.setattr(catalog_reader, "get_catalog_reader", lambda: reader)
    monkeypatch.setattr(sources, "get_catalog_reader", lambda: reader)
    bus = AsyncMock()
    monkeypatch.setattr(series_api, "get_event_bus", lambda: bus)
    monkeypatch.setattr(ProviderArtworkClient, "download_cover", AsyncMock(return_value=False))
    page = release_page
    page.goto(f"{seeded_server}/whats-new")
    page.get_by_test_id("whats-new-find-add").first.click()
    finder = page.get_by_test_id("whats-new-find-dialog")
    query = finder.get_by_label("Series title")
    expect(query).to_have_value("Atlas Deluxe")
    query.fill("Dark Knight")
    with page.expect_response(
        lambda response: (
            "/whats-new/find-series/" in response.url and "q=Dark+Knight" in response.url
        )
    ) as searched:
        finder.get_by_role("button", name="Search", exact=True).click()
    search_response = searched.value
    assert search_response.status == 200, search_response.text()
    assert any(row["title"] == "Batman" for row in search_response.json()["search_results"])
    result = finder.locator("article").filter(has_text="Batman (2016)")
    expect(result.get_by_role("button", name="Select", exact=True)).to_be_enabled()
    result.get_by_role("button", name="Select", exact=True).click()
    dialog = page.get_by_test_id("add-series-dialog")
    add = dialog.get_by_role("button", name="Add series", exact=True)
    expect(add).to_be_enabled()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/series") and response.request.method == "POST"
        )
    ) as added:
        add.click()
    response = added.value
    assert response.status == 201, response.text()
    record = response.json()
    assert record["comicvine_id"] == 10
    assert Path(record["path"]).is_dir() and bus.emit.await_count == 1

    def cleanup():
        result = page.request.delete(
            f"{seeded_server}/api/v1/series/{record['id']}",
            headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
        )
        assert result.status == 204, result.text()
        Path(record["path"]).rmdir()

    request.addfinalizer(cleanup)
    page.reload()
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    expect(row.get_by_test_id("whats-new-local-series")).to_have_attribute(
        "href", f"/series/{record['id']}"
    )
    expect(row).to_contain_text("Tracked" if record["monitored"] else "Paused")
    tracked = page.request.put(
        f"{seeded_server}/api/v1/series/{record['id']}",
        data={"monitored": True},
        headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
    )
    assert tracked.status == 200
    page.reload()
    expect(row).to_contain_text("Tracked")
    update = page.request.put(
        f"{seeded_server}/api/v1/series/{record['id']}",
        data={"monitored": False},
        headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
    )
    assert update.status == 200
    page.reload()
    expect(row).to_contain_text("Paused")
