"""The Add dialog binds one preview identity and revision, never browser metadata."""

from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


def preview(source="metron_api", identifier="42", **updates):
    result = {
        "source": source,
        "external_id": identifier,
        "source_revision": 7,
        "folder_preview": "Test publisher/Verified series [2024]",
        "series": {
            "status": "ok",
            "data": {
                "source": source,
                "identity_namespace": "metron"
                if source == "metron_api"
                else "gcd"
                if source.startswith("gcd_")
                else "comicvine",
                "external_id": identifier,
                "title": "Verified series",
                "year_start": 2024,
                "publisher": "Test publisher",
                "issue_count": 12,
            },
        },
        "issues": {"status": "ok", "data": {"results": [], "total": 12, "next_page": 2}},
    }
    result.update(updates)
    return result


def open_result(page, source="metron_api", identifier="42"):
    page.evaluate(
        "payload => selectResult(payload)",
        {
            "source": source,
            "externalId": identifier,
            "title": "Search title",
            "year": 2020,
            "publisher": "Search publisher",
        },
    )


@pytest.mark.parametrize("source", ["comicvine_local", "comicvine_api", "metron_api", "gcd_api_v2"])
def test_preview_then_add_sends_only_source_identity_revision_and_root(
    authed_page, seeded_server, source
):
    page = authed_page
    held = []
    adds = []
    page.route("**/api/v1/metadata/series/preview", lambda route: held.append(route))

    def added(route):
        adds.append(route.request.post_data_json)
        assert route.request.headers.get("x-csrf-token")
        route.fulfill(json={"id": 91, "title": "Verified series"})

    page.route("**/api/v1/series", added)
    page.goto(f"{seeded_server}/series/add")
    open_result(page, source)
    button = page.get_by_role("button", name="Add series", exact=True)
    expect(button).to_be_disabled()
    expect(page.get_by_test_id("add-series-preview-status")).to_contain_text("Loading preview")
    assert len(held) == 1
    root_id = page.evaluate("Alpine.$data(document.getElementById('add-series-app')).libraryRootId")
    assert held[0].request.post_data_json == {
        "source": source,
        "external_id": "42",
        "library_root_id": int(root_id),
    }
    assert held[0].request.headers.get("x-csrf-token")
    held[0].fulfill(json=preview(source))
    expect(page.get_by_test_id("add-series-dialog")).to_contain_text("Verified series (2024)")
    expect(button).to_be_enabled()
    expect(page.get_by_test_id("add-series-folder-preview")).to_have_text(
        "Test publisher/Verified series [2024]"
    )
    button.click()
    expect(page.get_by_test_id("add-series-dialog")).not_to_be_visible()
    assert adds == [
        {
            "source": source,
            "external_id": "42",
            "source_revision": 7,
            "library_root_id": adds[0]["library_root_id"],
        }
    ]
    assert isinstance(adds[0]["library_root_id"], int)


def test_partial_preview_keeps_profile_and_requires_successful_retry(authed_page, seeded_server):
    page = authed_page
    calls = []

    def respond(route):
        calls.append(True)
        result = preview()
        if len(calls) == 1:
            result["issues"] = {"status": "rate_limited", "retry_after_seconds": 60}
        route.fulfill(json=result)

    page.route("**/api/v1/metadata/series/preview", respond)
    page.goto(f"{seeded_server}/series/add")
    open_result(page)
    dialog = page.get_by_test_id("add-series-dialog")
    expect(dialog).to_contain_text("Verified series (2024)")
    expect(dialog).to_contain_text("60 seconds")
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_disabled()
    dialog.get_by_role("button", name="Retry preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
    assert len(calls) == 2


def test_closed_preview_cannot_overwrite_new_selection(authed_page, seeded_server):
    page = authed_page
    held = []
    page.route("**/api/v1/metadata/series/preview", lambda route: held.append(route))
    page.goto(f"{seeded_server}/series/add")
    open_result(page, identifier="41")
    expect(page.get_by_test_id("add-series-preview-status")).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    open_result(page, identifier="42")
    expect(page.get_by_test_id("add-series-preview-status")).to_be_visible()
    assert len(held) == 2
    held[1].fulfill(json=preview(identifier="42"))
    expect(page.get_by_test_id("add-series-dialog")).to_contain_text("Verified series (2024)")
    stale = preview(identifier="41")
    stale["series"]["data"]["title"] = "Stale series"
    with suppress(Exception):
        held[0].fulfill(json=stale)
    expect(page.get_by_test_id("add-series-dialog")).not_to_contain_text("Stale series")
    assert (
        page.evaluate("Alpine.$data(document.getElementById('add-series-app')).selectedExternalId")
        == "42"
    )


@pytest.mark.parametrize("failure", ["http", "identity", "revision", "issues", "folder"])
def test_invalid_preview_cannot_enable_add(authed_page, seeded_server, failure):
    page = authed_page
    result = preview()
    if failure == "identity":
        result["series"]["data"]["external_id"] = "999"
    elif failure == "revision":
        result["source_revision"] = -1
    elif failure == "issues":
        result["issues"] = {"status": "ok", "data": None}
    elif failure == "folder":
        result["folder_preview"] = None
    page.route(
        "**/api/v1/metadata/series/preview",
        lambda route: route.fulfill(
            status=409 if failure == "http" else 200,
            json={"detail": "Source settings changed. Preview again."}
            if failure == "http"
            else result,
        ),
    )
    page.goto(f"{seeded_server}/series/add")
    open_result(page)
    expect(page.get_by_role("button", name="Retry preview", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Add series", exact=True)).to_be_disabled()


def test_rejected_add_requires_new_preview_and_does_not_double_submit(authed_page, seeded_server):
    page = authed_page
    held = []
    previews = []

    def respond(route):
        previews.append(True)
        route.fulfill(json=preview())

    page.route("**/api/v1/metadata/series/preview", respond)
    page.route("**/api/v1/series", lambda route: held.append(route))
    page.goto(f"{seeded_server}/series/add")
    open_result(page)
    button = page.get_by_role("button", name="Add series", exact=True)
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_role("button", name="Adding...", exact=True)).to_be_disabled()
    page.evaluate("Alpine.$data(document.getElementById('add-series-app')).addSeriesToLibrary()")
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("add-series-dialog")).to_be_visible()
    assert len(held) == 1
    held[0].fulfill(status=409, json={"detail": "Source settings changed. Preview again."})
    expect(page.get_by_role("alert").filter(has_text="Source settings changed")).to_be_visible()
    expect(button).to_be_disabled()
    page.get_by_role("button", name="Retry preview", exact=True).click()
    expect(button).to_be_enabled()
    assert len(previews) == 2


def test_real_local_search_preview_add_refresh_and_existing_owner(
    authed_page, seeded_server, monkeypatch, tmp_path, request
):
    from pullbox.api.v1 import series as series_api
    from pullbox.providers.metadata import sources
    from pullbox.services.catalog import reader as catalog_reader
    from pullbox.services.provider_artwork import ProviderArtworkClient
    from tests.unit.test_catalog_reader import installed_reader

    reader = installed_reader(tmp_path)
    monkeypatch.setattr(catalog_reader, "get_catalog_reader", lambda: reader)
    monkeypatch.setattr(sources, "get_catalog_reader", lambda: reader)
    # This test owns the browser/database/folder workflow, not scheduled downloads.
    events = AsyncMock()
    monkeypatch.setattr(series_api, "get_event_bus", lambda: events)
    monkeypatch.setattr(ProviderArtworkClient, "download_cover", AsyncMock(return_value=False))
    page = authed_page
    page.goto(f"{seeded_server}/series/add?q=Dark+Knight")
    trigger = page.locator('[data-add-series-trigger="true"]').first
    expect(trigger).to_have_attribute("data-series-source", "comicvine_local")
    trigger.click()
    dialog = page.get_by_test_id("add-series-dialog")
    button = dialog.get_by_role("button", name="Add series", exact=True)
    expect(button).to_be_enabled()
    folder = page.get_by_test_id("add-series-folder-preview").inner_text()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/series") and response.request.method == "POST"
        )
    ) as added:
        button.click()
    response = added.value
    assert response.status == 201, response.text()
    record = response.json()

    def remove_test_series():
        result = page.request.delete(
            f"{seeded_server}/api/v1/series/{record['id']}",
            headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
        )
        assert result.status == 204, result.text()
        # Delete only the empty folder this test's Add created, not library data.
        Path(record["path"]).rmdir()

    request.addfinalizer(remove_test_series)
    assert record["comicvine_id"] == 10 and record["title"] == "Batman"
    assert Path(record["path"]).is_dir()
    assert Path(record["path"]).name == folder
    expect(dialog).not_to_be_visible()
    expect(page.get_by_test_id("add-series-existing-title-link")).to_have_attribute(
        "href", f"/series/{record['id']}"
    )
    assert events.emit.await_count == 1
    page.goto(f"{seeded_server}/series/{record['id']}")
    with (
        page.expect_navigation(
            url=f"{seeded_server}/series/{record['id']}", wait_until="domcontentloaded"
        ),
        page.expect_response(
            lambda response: response.url.endswith(f"/series/{record['id']}/refresh")
        ) as refreshed,
    ):
        page.get_by_test_id("series-action-refresh").click()
    assert refreshed.value.status == 200
    # The success action navigates away. Verify persisted state without waiting
    # for a response body attached to the previous document's fetch lifecycle.
    updated = page.request.get(f"{seeded_server}/api/v1/series/{record['id']}")
    assert updated.ok
    assert updated.json()["issue_catalog_state"] == "complete"
    expect(page.get_by_test_id("series-action-refresh")).to_be_enabled()
    detail = "A provider issue was renumbered. Review the issue match; existing files were kept."
    page.route(
        f"**/api/v1/series/{record['id']}/refresh",
        lambda route: route.fulfill(status=409, json={"detail": detail}),
    )
    page.get_by_test_id("series-action-refresh").click()
    expect(page.get_by_text(detail, exact=True)).to_be_visible()
    expect(page.get_by_test_id("series-action-refresh")).to_be_enabled()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 320)])
def test_preview_dialog_keyboard_reflow_and_accessibility(
    authed_page, seeded_server, browser_name, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    result = preview()
    result["series"]["data"]["title"] = '<img src=x onerror="window.injected=true">'
    page.route("**/api/v1/metadata/series/preview", lambda route: route.fulfill(json=result))
    page.goto(f"{seeded_server}/series/add")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_test_id("add-series-search-input")
    expect(trigger).to_be_visible()
    trigger.focus()
    expect(trigger).to_be_focused()
    open_result(page)
    dialog = page.get_by_test_id("add-series-dialog")
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
    assert page.evaluate("window.injected") is None
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    expect(dialog).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Close add series dialog")).to_be_focused()
    assert_no_axe_violations(
        page, name=f"source-add-{theme}-{width}", include=["[data-testid='add-series-dialog']"]
    )
    dialog.screenshot(
        path=f"output/playwright/source-add-{browser_name}-{theme}-{width}.png",
        animations="disabled",
    )
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390)])
def test_gcd_catalog_exception_is_explicit_and_bound_to_preview(
    authed_page, seeded_server, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900})
    result = preview("gcd_local", "3172")
    result["series"]["data"].update(title="Watchmen", identity_namespace="gcd", issue_count=13)
    result["issues"]["data"]["total"] = 13
    result["catalog_review"] = {
        "token": "a" * 64,
        "total": 13,
        "supported_count": 12,
        "excluded": [
            {
                "source": "gcd_local",
                "series_external_id": "3172",
                "external_id": "647784",
                "issue_number_text": "1 [2nd Printing]",
            }
        ],
    }
    adds = []
    page.route("**/api/v1/metadata/series/preview", lambda route: route.fulfill(json=result))

    def added(route):
        adds.append(route.request.post_data_json)
        route.fulfill(json={"id": 91, "title": "Watchmen"})

    page.route("**/api/v1/series", added)
    page.goto(f"{seeded_server}/series/add")
    page.evaluate("theme => applyTheme(theme)", theme)
    open_result(page, "gcd_local", "3172")
    dialog = page.get_by_test_id("add-series-dialog")
    expect(dialog.get_by_text("12 issues will be added; 1 left out.", exact=True)).to_be_visible()
    expect(dialog.get_by_text("1 [2nd Printing]", exact=True)).to_be_visible()
    button = dialog.get_by_role("button", name="Add 12 supported issues", exact=True)
    expect(button).to_be_enabled()
    assert not adds
    assert_no_axe_violations(
        page, name=f"gcd-review-{theme}", include=["[data-testid='add-series-dialog']"]
    )
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    button.click()
    expect(dialog).not_to_be_visible()
    assert adds[0]["catalog_review_token"] == "a" * 64
    assert "issues" not in adds[0] and "excluded" not in adds[0]


@pytest.mark.parametrize("supported", [0, 12])
def test_catalog_review_pages_and_cancel_do_not_create_series(
    authed_page, seeded_server, supported
):
    page = authed_page
    result = preview("gcd_local", "3172")
    result["series"]["data"]["identity_namespace"] = "gcd"
    result["catalog_review"] = {
        "token": "b" * 64,
        "total": supported + 23,
        "supported_count": supported,
        "excluded": [
            {
                "source": "gcd_local",
                "series_external_id": "3172",
                "external_id": str(6000 + n),
                "issue_number_text": f"{n} [2nd Printing]",
            }
            for n in range(23)
        ],
    }
    adds = []
    page.route("**/api/v1/metadata/series/preview", lambda route: route.fulfill(json=result))
    page.route("**/api/v1/series", lambda route: adds.append(route))
    page.goto(f"{seeded_server}/series/add")
    open_result(page, "gcd_local", "3172")
    dialog = page.get_by_test_id("add-series-dialog")
    expect(dialog.get_by_text("0 [2nd Printing]", exact=True)).to_be_visible()
    expect(dialog.get_by_role("link", name="View GCD issue")).to_have_count(10)
    dialog.get_by_role("button", name="Next entries").click()
    expect(dialog.get_by_text("10 [2nd Printing]", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Next entries").click()
    expect(dialog.get_by_role("link", name="View GCD issue")).to_have_count(3)
    expect(dialog.get_by_role("button", name="Next entries")).to_be_disabled()
    if not supported:
        expect(dialog.get_by_role("button", name="Add 0 supported issues")).to_be_disabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert not adds
