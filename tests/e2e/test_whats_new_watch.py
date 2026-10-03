"""Real Watch save/cancel and explicit completion, without replacing page content."""
# ruff: noqa: F811 - reuse the owning discovery/browser fixture.

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete, select

from pullbox.database import get_session_factory
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.series_interest import SeriesInterest, SeriesInterestState
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.whats_new_refresh_queue import run_whats_new_refresh
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_whats_new_find_add import release_page  # noqa: F401

pytestmark = pytest.mark.e2e


async def future_fixture():
    async with get_session_factory().begin() as session:
        await session.execute(delete(SeriesInterest))
        row = await session.scalar(select(WhatsNewReleaseCache))
        releases = [{**item, "store_date": "2099-01-01"} for item in row.payload["issues"]]
        row.store_date = date(2099, 1, 1)
        row.payload = {**row.payload, "store_date": "2099-01-01", "issues": releases}


@pytest.fixture
def watch_page(release_page):
    async def snapshot():
        async with get_session_factory()() as session:
            roots = (await session.scalars(select(LibraryRoot))).all()
            setting = await session.get(SystemConfig, "search_on_add_default")
            return {root.id: root.is_default_managed_destination for root in roots}, (
                setting.value if setting else None
            )

    defaults, add_setting = _run_async_blocking(snapshot())
    _run_async_blocking(future_fixture())
    yield release_page

    async def cleanup():
        async with get_session_factory().begin() as session:
            await session.execute(delete(SeriesInterest))
            for root_id, default in defaults.items():
                root = await session.get(LibraryRoot, root_id)
                if root:
                    root.is_default_managed_destination = default
            setting = await session.get(SystemConfig, "search_on_add_default")
            if add_setting is None and setting is not None:
                await session.delete(setting)
            elif setting is not None:
                setting.value = add_setting

    _run_async_blocking(cleanup())


def test_watch_reload_pull_list_cancel_and_rewatch(watch_page, seeded_server):
    page = watch_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/whats-new")
    table = page.get_by_test_id("whats-new-current-release-table")
    table.evaluate("el => { el.dataset.checkpoint = 'unchanged'; }")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row).to_contain_text("Watching")
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_enabled()
    assert not errors
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_focused()
    expect(table).to_have_attribute("data-checkpoint", "unchanged")
    held = []
    page.route("**/api/v1/whats-new/watch/*/cancel", lambda route: held.append(route))
    cancel = row.get_by_role("button", name="Cancel Watch", exact=True)
    cancel.click()
    expect(cancel).to_be_disabled()
    assert len(held) == 1
    held.pop().continue_()
    expect(row.get_by_role("button", name="Watch", exact=True)).to_be_focused()
    page.unroute("**/api/v1/whats-new/watch/*/cancel")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_enabled()
    page.reload()
    expect(row).to_contain_text("Watching")
    page.goto(f"{seeded_server}/pull-list")
    section = page.get_by_test_id("pull-list-watching")
    expect(section).to_contain_text("Atlas Deluxe")
    expect(section).to_contain_text("Use Find & Add")
    watched = page.get_by_test_id("pull-list-watch-row").filter(has_text="Atlas Deluxe")
    watched.get_by_role("button", name="Cancel Watch", exact=True).click()
    expect(watched).not_to_be_visible()
    expect(section.get_by_role("heading", name="Watching", exact=True)).to_be_focused()
    page.reload()
    expect(
        page.get_by_test_id("pull-list-watch-row").filter(has_text="Atlas Deluxe")
    ).to_have_count(0)
    page.goto(f"{seeded_server}/whats-new")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row).to_contain_text("Watching")


async def publish_watched_fixture():
    factory = get_session_factory()
    async with factory.begin() as session:
        cached = await session.scalar(select(WhatsNewReleaseCache))
        releases = [
            {**release, "store_date": date.today().isoformat()}
            for release in cached.payload["issues"]
        ]
        await session.execute(delete(WhatsNewReleaseCache))

    class Client:
        async def get_current_week(self):
            return {"store_date": date.today().isoformat(), "issues": releases}

        async def get_upcoming(self):
            return {"weeks": []}

    await run_whats_new_refresh(session_factory=factory, client=Client())
    async with factory() as session:
        assert (
            await session.scalar(select(SeriesInterest))
        ).state is SeriesInterestState.NEEDS_CONFIRMATION


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("monitor", 320)])
def test_refresh_confirmation_has_actionable_release_link_and_cancel(
    watch_page, seeded_server, theme, width
):
    page = watch_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_enabled()
    _run_async_blocking(publish_watched_fixture())
    page.reload()
    expect(row).to_contain_text("Needs confirmation")
    page.goto(f"{seeded_server}/pull-list")
    page.evaluate("theme => applyTheme(theme)", theme)
    watched = page.get_by_test_id("pull-list-watch-row").filter(has_text="Atlas Deluxe")
    expect(watched).to_contain_text("Needs confirmation")
    expect(watched).not_to_contain_text("Next release 2099")
    assert_no_axe_violations(page, name=f"watch-confirmation-{theme}-{width}")
    watched.get_by_role("link", name="Find & Add", exact=True).click()
    expect(page.get_by_test_id("whats-new-current-release-table")).to_be_visible()
    row.get_by_test_id("whats-new-find-add").click()
    finder = page.get_by_test_id("whats-new-find-dialog")
    expect(finder.get_by_label("Series title")).to_have_value("Atlas Deluxe")
    finder.get_by_role("button", name="Cancel", exact=True).click()
    row.get_by_role("button", name="Cancel Watch", exact=True).click()
    expect(row).not_to_contain_text("Needs confirmation")
    expect(row.get_by_role("button", name="Watch", exact=True)).to_have_count(0)
    page.reload()
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_have_count(0)


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("monitor", 320)])
def test_watching_controls_reflow_accessibility_and_cancel_focus(
    watch_page, seeded_server, theme, width, browser_name
):
    page = watch_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    page.evaluate("theme => applyTheme(theme)", theme)
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row).to_contain_text("Watching")
    row.get_by_role("button", name="Cancel Watch", exact=True).click()
    expect(row.get_by_role("button", name="Watch", exact=True)).to_be_focused()
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_enabled()
    page.goto(f"{seeded_server}/pull-list")
    expect(
        page.get_by_test_id("pull-list-watch-row").filter(has_text="Atlas Deluxe")
    ).to_be_visible()
    assert_no_axe_violations(page, name=f"watch-{theme}-{width}")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.screenshot(path=f"output/playwright/watch-{browser_name}-{theme}-{width}.png")


def test_watch_without_default_requires_library_choice_and_preserves_cancel_focus(
    watch_page, seeded_server
):
    async def unset_default():
        async with get_session_factory().begin() as session:
            roots = (await session.scalars(select(LibraryRoot))).all()
            root = next(root for root in roots if root.is_default_managed_destination)
            root.is_default_managed_destination = False
            return root.name

    root_name = _run_async_blocking(unset_default())
    page = watch_page
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    trigger = row.get_by_role("button", name="Watch", exact=True)
    trigger.click()
    dialog = page.get_by_test_id("watch-root-dialog")
    expect(dialog).to_be_focused()
    expect(dialog.get_by_role("button", name="Watch", exact=True)).to_be_disabled()
    assert_no_axe_violations(page, name="watch-root", include=["[data-testid='watch-root-dialog']"])
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()
    trigger.click()
    dialog.get_by_label("Library destination").click()
    page.get_by_role("option").filter(has_text=root_name).click()
    dialog.get_by_role("button", name="Watch", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(row).to_contain_text("Watching")
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_focused()


@pytest.mark.parametrize("available", [False, True])
def test_real_watched_series_find_add_enables_monitoring_and_leaves_watch_list(
    watch_page, seeded_server, monkeypatch, tmp_path, request, available
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

    async def pause_ordinary_add():
        async with get_session_factory().begin() as session:
            setting = await session.get(SystemConfig, "search_on_add_default")
            if setting is None:
                session.add(SystemConfig(key="search_on_add_default", value="false"))
            else:
                setting.value = "false"

    _run_async_blocking(pause_ordinary_add())
    page = watch_page
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    row.get_by_role("button", name="Watch", exact=True).click()
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_be_enabled()
    if available:
        _run_async_blocking(publish_watched_fixture())
        page.reload()
        expect(row).to_contain_text("Needs confirmation")
    row.get_by_test_id("whats-new-find-add").click()
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
    assert searched.value.status == 200, searched.value.text()
    finder.locator("article").filter(has_text="Batman (2016)").get_by_role(
        "button", name="Select", exact=True
    ).click()
    dialog = page.get_by_test_id("add-series-dialog")
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/series") and response.request.method == "POST"
        )
    ) as added:
        dialog.get_by_role("button", name="Add series", exact=True).click()
    response = added.value
    assert response.status == 201, response.text()
    record = response.json()

    def cleanup():
        deleted = page.request.delete(
            f"{seeded_server}/api/v1/series/{record['id']}",
            headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
        )
        assert deleted.status == 204, deleted.text()
        Path(record["path"]).rmdir()

    request.addfinalizer(cleanup)
    assert record["monitored"] is True and bus.emit.await_count == 1
    expect(row).to_contain_text("Tracked")
    page.reload()
    expect(row).to_contain_text("Tracked")
    expect(row.get_by_role("button", name="Cancel Watch", exact=True)).to_have_count(0)
    page.goto(f"{seeded_server}/pull-list")
    expect(
        page.get_by_test_id("pull-list-watch-row").filter(has_text="Atlas Deluxe")
    ).to_have_count(0)
