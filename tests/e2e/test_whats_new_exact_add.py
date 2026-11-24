"""Direct Add uses the real packaged catalog flow without opening the finder."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.core.events import EventBus
from pullbox.database import get_session_factory
from pullbox.models import LibraryRoot, Series, SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.providers.metadata import sources
from pullbox.services import whats_new_refresh_queue
from pullbox.services.catalog import reader as catalog_reader
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_whats_new_find_add import release_page  # noqa: F401
from tests.unit.test_catalog_reader import installed_reader

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def isolated_release_feed():
    # Do not race a real upstream startup refresh against the seeded public feed.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(whats_new_refresh_queue, "refresh_whats_new_cache_if_needed", AsyncMock())
        yield


@pytest.mark.parametrize(("theme", "width"), [("light", 1280), ("dark", 390), ("monitor", 320)])
def test_add_published_series_in_place(
    release_page,  # noqa: F811
    seeded_server,
    monkeypatch,
    tmp_path,
    theme,
    width,
):
    reader = installed_reader(tmp_path)
    monkeypatch.setattr(catalog_reader, "get_catalog_reader", lambda: reader)
    monkeypatch.setattr(sources, "get_catalog_reader", lambda: reader)
    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)

    async def seed():
        async with get_session_factory().begin() as session:
            row = await session.scalar(select(WhatsNewReleaseCache))
            row.fetched_at = datetime.now(UTC)
            release = row.payload["issues"][0]
            row.payload = {
                **row.payload,
                "issues": [
                    {
                        **release,
                        "series": {
                            **release["series"],
                            "comicvine_series_id": 10,
                            "publication_state": "published",
                            "publication_as_of": row.fetched_at.date().isoformat(),
                        },
                    },
                    *row.payload["issues"][1:],
                ],
            }
            await WhatsNewCacheService().upsert_upcoming(session, payload={"weeks": []})
            root = await session.scalar(
                select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True))
            )
            assert root is not None

    _run_async_blocking(seed())
    page = release_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    page.evaluate("theme => applyTheme(theme)", theme)
    table = page.get_by_test_id("whats-new-current-release-table")
    table.evaluate("el => el.dataset.sameTable = 'yes'")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    no_id = page.get_by_test_id("whats-new-release-row").filter(has_text="Budget Hero")
    expect(no_id.get_by_role("button", name="Add Series", exact=True)).to_have_count(0)
    expect(no_id.get_by_role("button", name="Find & Add")).to_be_visible()
    assert_no_axe_violations(
        page,
        name=f"release-direct-add-{theme}",
        include=['[data-testid="whats-new-current-release-table"]'],
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    with page.expect_response("**/api/v1/whats-new/add-series") as pending:
        row.get_by_role("button", name="Add Series", exact=True).press("Enter")
    assert pending.value.ok, pending.value.text()
    link = row.get_by_test_id("whats-new-local-series")
    expect(link).to_be_focused()
    expect(table).to_have_attribute("data-same-table", "yes")
    expect(page.get_by_test_id("whats-new-find-dialog")).not_to_be_visible()
    href = link.get_attribute("href")
    emit.assert_awaited_once()
    page.reload()
    expect(link).to_have_attribute("href", href)
    link.click()
    expect(page).to_have_url(seeded_server + href)
    series_url = seeded_server + "/api/v1" + href
    path = Path(page.request.get(series_url).json()["path"])
    assert (
        page.request.delete(
            series_url, headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")}
        ).status
        == 204
    )
    path.rmdir()


def test_existing_native_owner_opens_locally_without_guessing_the_issue(
    release_page,  # noqa: F811
    seeded_server,
):
    async def seed():
        async with get_session_factory().begin() as db:
            row = await WhatsNewCacheService().get_latest_current_week(db)
            row.fetched_at = datetime.now(UTC)
            release, *others = row.payload["issues"]
            row.payload = {
                **row.payload,
                "issues": [
                    {**release, "series": {**release["series"], "metron_series_id": 810002}},
                    *others,
                ],
            }
            await WhatsNewCacheService().upsert_upcoming(db, payload={"weeks": []})
            owner = Series(title="Native edition", sort_title="native edition", monitored=False)
            db.add(owner)
            await db.flush()
            db.add(
                SeriesExternalIdentity(
                    series_id=owner.id,
                    identity_namespace="metron",
                    external_id="810002",
                    verification_state="verified",
                    evidence_kind="user_selection",
                )
            )
            return owner.id

    owner_id = _run_async_blocking(seed())
    page = release_page
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    expect(row.get_by_text("Paused", exact=True)).to_be_visible()
    expect(row.get_by_role("button", name="Confirm series link")).to_be_visible()
    expect(row.get_by_role("button", name="Grab", exact=True)).to_have_count(0)
    link = row.get_by_role("link", name="Added", exact=True)
    expect(link).to_have_attribute("href", f"/series/{owner_id}")
    link.press("Enter")
    expect(page).to_have_url(f"{seeded_server}/series/{owner_id}")
    assert page.request.delete(
        f"{seeded_server}/api/v1/series/{owner_id}",
        headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
    ).ok
