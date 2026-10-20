"""Series corrections preserve drafts, require review, and never edit comic files."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


def _field(key, label, current, **kwargs):
    return {
        "key": f"series.{key}",
        "label": label,
        "current": current,
        "proposed": current,
        "editable": True,
        "input_type": "text",
        "overridden": False,
        "selected": False,
        "changed": False,
        "can_clear": True,
        "reason": "",
        "options": [],
        "sources": [{"token": "library", "label": "Library", "value": current}],
        **kwargs,
    }


@pytest.fixture
def editor_api(authed_page):
    page = authed_page
    fields = [
        _field("title", "Title", "Batman", can_clear=False),
        _field("sort_title", "Sort title", "Batman", overridden=True, can_clear=False),
        _field("description", "Description", "Keep this", input_type="textarea", overridden=True),
        _field("year_start", "Start year", "2016", input_type="number"),
        _field(
            "status",
            "Status",
            "continuing",
            input_type="select",
            can_clear=False,
            options=[
                {"value": "continuing", "label": "Continuing"},
                {"value": "ended", "label": "Ended"},
            ],
        ),
        _field(
            "issue_count",
            "Issue count",
            "3",
            editable=False,
            reason="Issue inventory is protected.",
        ),
        _field(
            "identities",
            "Provider IDs",
            "ComicVine 12345",
            editable=False,
            reason="Use Link provider to manage identities.",
        ),
    ]
    state = {"job": None, "library_saved": False, "targets": [], "retryable": False}
    calls = {
        "previews": [],
        "writes": [],
        "retries": [],
        "cancels": [],
        "state": state,
        "fields": fields,
    }

    def preview(route):
        body = route.request.post_data_json
        calls["previews"].append(body)
        edits = body["edits"]
        if edits.get("title", {}).get("value") == "Rejected":
            route.fulfill(status=409, json={"detail": "The series changed. Preview again."})
            return
        updated = []
        for field in fields:
            edit = edits.get(field["key"].removeprefix("series."))
            proposed = field["current"]
            if edit:
                proposed = "" if edit["action"] == "clear" else str(edit.get("value", proposed))
            sources = [
                source
                for source in field["sources"]
                if body["mode"] != "library_only" or source["token"] == "library"
            ]
            updated.append(
                {
                    **field,
                    "selected": bool(edit),
                    "changed": bool(edit),
                    "proposed": proposed,
                    "sources": sources,
                }
            )
        route.fulfill(
            json={
                "series_id": 1,
                "mode": body["mode"],
                "editor_fields": updated,
                "editor_changed": bool(edits),
                "ready": bool(edits),
                "review_key": "a" * 64,
                "targets": []
                if body["mode"] == "library_only"
                else [
                    {"directory": "Batman", "action": "create", "reason": "", "changes": []},
                    {
                        "directory": "Shared folder",
                        "action": "blocked",
                        "reason": "Contains another series.",
                        "changes": [],
                    },
                ],
            }
        )

    def write(route):
        calls["writes"].append(route.request.post_data_json)
        state.update(
            {
                "job": {
                    "id": "series-editor-test",
                    "state": "RUNNING",
                    "percent": 42,
                    "message": "Writing eligible series.json files",
                    "error": None,
                },
                "library_saved": True,
            }
        )
        route.fulfill(
            status=202,
            json={"job_id": "series-editor-test", "state": "RUNNING", "library_saved": True},
        )

    def retry(route):
        calls["retries"].append(route.request.post_data_json)
        state.update(
            {
                "job": {
                    "id": "series-editor-retry",
                    "state": "RUNNING",
                    "percent": 10,
                    "message": "Retrying unfinished sidecars",
                    "error": None,
                },
                "retryable": False,
            }
        )
        route.fulfill(
            status=202,
            json={"job_id": "series-editor-retry", "state": "RUNNING", "library_saved": True},
        )

    def cancel(route):
        calls["cancels"].append(route.request.url)
        state["job"]["state"] = "CANCELLING"
        route.fulfill(json={})

    page.route("**/api/v1/series/1/metadata-editor/preview", preview)
    page.route("**/api/v1/series/1/metadata-editor/write", write)
    page.route("**/api/v1/series/1/metadata-editor/job", lambda route: route.fulfill(json=state))
    page.route("**/api/v1/series/1/metadata-editor/retry", retry)
    page.route("**/api/v1/utilities/jobs/*/cancel", cancel)
    return calls


def _open(page, base_url):
    page.goto(f"{base_url}/series/1")
    trigger = page.get_by_role("button", name="Edit metadata", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Edit series metadata", exact=True)
    expect(dialog).to_be_visible()
    return dialog


def _source(page, dialog, label, choice):
    dialog.get_by_label(f"Update {label}", exact=True).check()
    dialog.get_by_role("button", name=f"Value source for {label}", exact=True).click()
    page.get_by_role("option", name=choice, exact=True).click()


def test_editor_is_available_on_series_without_a_conflict(authed_page, seeded_server):
    page = authed_page
    page.goto(f"{seeded_server}/series/1")
    trigger = page.get_by_role("button", name="Edit metadata", exact=True)
    expect(trigger).to_be_visible()
    trigger.click()
    dialog = page.get_by_role("dialog", name="Edit series metadata", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Comic files and folder names stay unchanged.")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(trigger).to_be_focused()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_editor_preview_exact_inputs_typed_controls_and_themes(
    authed_page, seeded_server, editor_api, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 740})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    dialog = _open(page, seeded_server)
    page.evaluate("theme => applyTheme(theme)", theme)
    page.get_by_test_id("series-detail-hero").evaluate("node => { node.keepHero = true; }")
    expect(dialog.get_by_text("No differences to review.")).to_be_visible()
    expect(
        dialog.get_by_text(
            "Library changes save first; cancelling stops only unfinished sidecar writes."
        )
    ).to_be_visible()
    expect(
        dialog.get_by_label("Library and eligible series.json files", exact=True)
    ).to_be_checked()
    dialog.get_by_role("button", name="All fields", exact=True).click()
    expect(dialog.get_by_text("Issue inventory is protected.")).to_be_visible()
    expect(dialog.get_by_label("Update Issue count", exact=True)).to_have_count(0)
    dialog.get_by_role("button", name="Select all eligible", exact=True).click()
    expect(dialog.get_by_label("Update Description", exact=True)).to_be_checked()
    dialog.get_by_role("button", name="Deselect all", exact=True).click()
    _source(page, dialog, "Title", "Enter correction")
    dialog.get_by_label("New Title", exact=True).fill("Rejected")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("series changed")
    expect(dialog.get_by_label("New Title", exact=True)).to_have_value("Rejected")
    dialog.get_by_label("New Title", exact=True).fill("My corrected title")
    _source(page, dialog, "Status", "Enter correction")
    dialog.get_by_role("button", name="New Status", exact=True).click()
    page.get_by_role("option", name="Ended", exact=True).click()
    _source(page, dialog, "Start year", "Enter correction")
    dialog.get_by_label("New Start year", exact=True).fill("2020")
    _source(page, dialog, "Description", "Clear value")
    _source(page, dialog, "Sort title", "Remove manual override")
    dialog.get_by_role("button", name="Differences", exact=True).click()
    update = dialog.get_by_role("button", name="Update preview", exact=True)
    apply = dialog.get_by_role("button", name="Apply selected changes", exact=True)
    expect(apply).to_be_disabled()
    update.click()
    expect(apply).to_be_enabled()
    expected_edits = {
        "title": {"action": "set", "value": "My corrected title"},
        "status": {"action": "set", "value": "ended"},
        "year_start": {"action": "set", "value": 2020},
        "description": {"action": "clear"},
        "sort_title": {"action": "reset"},
    }
    assert editor_api["previews"][-1] == {"mode": "library_and_sidecars", "edits": expected_edits}
    expect(dialog.get_by_text("Contains another series.")).to_be_visible()
    dialog.get_by_label("Library only", exact=True).check()
    expect(apply).to_be_disabled()
    update.click()
    expect(apply).to_be_enabled()
    expect(dialog.get_by_text("Contains another series.")).to_be_hidden()
    assert editor_api["previews"][-1]["mode"] == "library_only"
    dialog.get_by_label("Library and eligible series.json files", exact=True).check()
    update.click()
    expect(apply).to_be_enabled()
    assert_no_axe_violations(
        page, name=f"series-editor-{theme}", include=['[aria-labelledby="series-editor-title"]']
    )
    bounds = dialog.bounding_box()
    assert bounds and bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 740
    assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    assert dialog.locator(".modal-body").evaluate("node => node.scrollHeight > node.clientHeight")
    dialog.locator(".modal-body").evaluate("node => { node.scrollTop = 0; }")
    page.screenshot(
        path=str(
            Path(__file__).resolve().parents[2] / f"output/playwright/series-editor/{theme}.png"
        )
    )
    page.evaluate("window.editorShellMarker = true")
    page.get_by_test_id("series-detail-title").evaluate(
        "node => { node.textContent = 'Stale title'; }"
    )
    apply.click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "42")
    expect(dialog).to_contain_text("Library changes are saved.")
    assert editor_api["writes"] == [
        {"mode": "library_and_sidecars", "edits": expected_edits, "review_key": "a" * 64}
    ]
    assert page.get_by_test_id("series-detail-hero").evaluate("node => node.keepHero")
    expect(page.get_by_test_id("series-detail-title")).to_contain_text("BATMAN")
    assert page.evaluate("window.editorShellMarker")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(page.get_by_role("button", name="Edit metadata", exact=True)).to_be_focused()
    expect(page.get_by_role("button", name="Write series metadata", exact=True)).to_be_visible()
    assert not errors


def test_editor_write_error_retains_draft_and_requires_new_preview(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    page.route(
        "**/api/v1/series/1/metadata-editor/write",
        lambda route: route.fulfill(status=409, json={"detail": "Sidecar changed. Preview again."}),
    )
    dialog = _open(page, seeded_server)
    dialog.get_by_role("button", name="All fields", exact=True).click()
    _source(page, dialog, "Title", "Enter correction")
    dialog.get_by_label("New Title", exact=True).fill("My correction")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    apply = dialog.get_by_role("button", name="Apply selected changes", exact=True)
    apply.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Sidecar changed")
    expect(dialog.get_by_label("New Title", exact=True)).to_have_value("My correction")
    expect(apply).to_be_disabled()
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Edit metadata", exact=True).click()
    expect(dialog.get_by_label("New Title", exact=True)).to_have_value("My correction")
    expect(apply).to_be_disabled()
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(apply).to_be_enabled()
    dialog.get_by_label("New Title", exact=True).fill("Changed after preview")
    expect(apply).to_be_disabled()


def test_editor_saved_library_partial_sidecars_retry_and_cancel(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    editor_api["state"].update(
        {
            "job": {
                "id": "partial-write",
                "state": "FAILED",
                "percent": 100,
                "message": "Some series.json files need attention.",
                "error": "Folder is not writable.",
            },
            "library_saved": True,
            "retryable": True,
            "targets": [
                {"directory": "Batman", "state": "failed", "reason": "Folder is not writable."}
            ],
        }
    )
    dialog = _open(page, seeded_server)
    expect(dialog).to_contain_text("Library changes are saved.")
    expect(dialog.get_by_role("alert")).to_contain_text("Folder is not writable.")
    dialog.get_by_role("button", name="Retry unfinished sidecars", exact=True).click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "10")
    assert editor_api["retries"] == [{"job_id": "partial-write"}]
    assert not editor_api["writes"]
    assert not editor_api["previews"]
    dialog.get_by_role("button", name="Cancel sidecar write", exact=True).click()
    expect(dialog.get_by_role("button", name="Cancel sidecar write", exact=True)).to_be_disabled()
    assert editor_api["cancels"][0].endswith("/series-editor-retry/cancel")
    expect(dialog).to_contain_text("Library changes are saved.")


def test_editor_keyboard_escape_does_not_close_dialog_for_dropdown(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    dialog = _open(page, seeded_server)
    dialog.get_by_role("button", name="All fields", exact=True).click()
    dialog.get_by_label("Update Title", exact=True).check()
    source = dialog.get_by_role("button", name="Value source for Title", exact=True)
    source.press("Enter")
    page.get_by_role("option", name="Enter correction", exact=True).press("Escape")
    expect(dialog).to_be_visible()
    expect(source).to_be_focused()
    source.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("button", name="Edit metadata", exact=True)).to_be_focused()


def test_editor_saved_sources_are_tokens_and_library_only_excludes_sidecar(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    token = "b" * 64
    editor_api["fields"][0]["sources"].append(
        {"token": token, "label": "series.json (Batman)", "value": "Saved sidecar title"}
    )
    dialog = _open(page, seeded_server)
    expect(dialog.get_by_label("Update Title", exact=True)).to_be_visible()
    _source(page, dialog, "Title", "series.json (Batman)")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_enabled()
    assert editor_api["previews"][-1]["edits"] == {"title": {"action": "sidecar", "source": token}}
    dialog.get_by_label("Library only", exact=True).check()
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text(
        "Sidecar values are not available with Library only"
    )
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_disabled()
    _source(page, dialog, "Title", "Keep library value")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_enabled()
    assert editor_api["previews"][-1] == {
        "mode": "library_only",
        "edits": {"title": {"action": "library"}},
    }


def test_editor_completed_job_can_start_a_fresh_review(authed_page, seeded_server, editor_api):
    page = authed_page
    editor_api["state"].update(
        {
            "job": {
                "id": "completed-write",
                "state": "COMPLETED",
                "percent": 100,
                "message": "Library and series.json files saved.",
                "error": None,
            },
            "library_saved": True,
            "retryable": False,
            "targets": [{"directory": "Batman", "state": "COMPLETED", "reason": None}],
        }
    )
    dialog = _open(page, seeded_server)
    expect(dialog.get_by_text("Written", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Preview again", exact=True).click()
    expect(dialog.get_by_role("button", name="All fields", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_disabled()
    assert editor_api["previews"] == [{"mode": "library_and_sidecars", "edits": {}}]


def test_editor_completed_job_with_partial_targets_is_not_blanket_success(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    editor_api["state"].update(
        {
            "job": {
                "id": "e" * 32,
                "state": "COMPLETED",
                "percent": 100,
                "message": "Library saved; 1 sidecar needs attention.",
                "error": None,
            },
            "library_saved": True,
            "partial": True,
            "retryable": True,
            "targets": [
                {"directory": "Batman", "state": "COMPLETED", "reason": None},
                {
                    "directory": "Batman other root",
                    "state": "FAILED",
                    "reason": "Folder is not writable.",
                },
            ],
        }
    )
    dialog = _open(page, seeded_server)
    expect(dialog.get_by_text("Written", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Needs attention", exact=True)).to_be_visible()
    expect(
        dialog.get_by_text("Some series.json files still need attention.", exact=False)
    ).to_be_visible()
    expect(dialog.get_by_text("Folder is not writable.", exact=True)).to_be_visible()
    expect(
        dialog.get_by_role("button", name="Retry unfinished sidecars", exact=True)
    ).to_be_enabled()


def test_editor_series_navigation_initializes_and_keyboard_focus(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/series")
    series_link = page.locator('a[href="/series/1"]').first
    expect(series_link).to_have_attribute("hx-boost", "false")
    series_link.click()
    trigger = page.get_by_role("button", name="Edit metadata", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Edit series metadata", exact=True)
    expect(dialog.get_by_role("button", name="All fields", exact=True)).to_be_visible()
    dialog.focus()
    dialog.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Update preview", exact=True)).not_to_be_focused()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_focused()
    dialog.press("Escape")
    expect(trigger).to_be_focused()
    assert not errors


def test_editor_late_preview_cannot_approve_changed_inputs(authed_page, seeded_server, editor_api):
    page = authed_page
    dialog = _open(page, seeded_server)
    dialog.get_by_role("button", name="All fields", exact=True).click()
    _source(page, dialog, "Title", "Enter correction")
    dialog.get_by_label("New Title", exact=True).fill("Reviewed text")
    pending = []
    page.route("**/api/v1/series/1/metadata-editor/preview", lambda route: pending.append(route))
    with page.expect_request("**/api/v1/series/1/metadata-editor/preview"):
        dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_label("New Title", exact=True)).to_be_disabled()
    page.get_by_test_id("series-metadata-editor").evaluate(
        "node => { Alpine.$data(node).drafts['series.title'].value = 'Changed while checking'; }"
    )
    pending[0].fulfill(
        json={
            "series_id": 1,
            "mode": "library_and_sidecars",
            "editor_fields": editor_api["fields"],
            "editor_changed": True,
            "ready": True,
            "review_key": "a" * 64,
            "targets": [],
        }
    )
    expect(dialog.get_by_label("New Title", exact=True)).to_be_enabled()
    expect(dialog.get_by_label("New Title", exact=True)).to_have_value("Changed while checking")
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Update preview", exact=True)).to_be_enabled()


def test_editor_real_api_previews_typed_library_only_corrections(authed_page, seeded_server):
    page = authed_page
    dialog = _open(page, seeded_server)
    expect(dialog.get_by_role("button", name="All fields", exact=True)).to_be_visible()
    dialog.get_by_label("Library only", exact=True).check()
    dialog.get_by_role("button", name="All fields", exact=True).click()
    _source(page, dialog, "Description", "Enter correction")
    dialog.get_by_label("New Description", exact=True).fill("A corrected series description.")
    _source(page, dialog, "Status", "Enter correction")
    dialog.get_by_role("button", name="New Status", exact=True).click()
    page.get_by_role("option", name="Ended", exact=True).click()
    with page.expect_response("**/api/v1/series/1/metadata-editor/preview") as response:
        dialog.get_by_role("button", name="Update preview", exact=True).click()
    assert response.value.status == 200
    body = response.value.json()
    assert body["mode"] == "library_only" and body["targets"] == []
    assert (
        next(field for field in body["editor_fields"] if field["key"] == "series.description")[
            "proposed"
        ]
        == "A corrected series description."
    )
    assert (
        next(field for field in body["editor_fields"] if field["key"] == "series.status")[
            "proposed"
        ]
        == "ended"
    )
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_enabled()
    expect(dialog.get_by_role("alert")).to_be_hidden()


def test_editor_empty_enum_shows_placeholder_until_a_value_is_chosen(
    authed_page, seeded_server, editor_api
):
    page = authed_page
    editor_api["fields"].append(
        _field(
            "archive_format",
            "Publication format",
            "",
            input_type="select",
            options=[
                {"value": "Limited Series", "label": "Limited Series"},
                {"value": "Digital Chapter", "label": "Digital Chapter"},
            ],
        )
    )
    dialog = _open(page, seeded_server)
    dialog.get_by_role("button", name="All fields", exact=True).click()
    _source(page, dialog, "Publication format", "Enter correction")
    value = dialog.get_by_role("button", name="New Publication format", exact=True)
    expect(value).to_contain_text("Choose a value")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("Choose a value for Publication format.")
    assert len(editor_api["previews"]) == 1
    value.click()
    page.get_by_role("option", name="Digital Chapter", exact=True).click()
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_enabled()
    assert editor_api["previews"][-1]["edits"] == {
        "archive_format": {"action": "set", "value": "Digital Chapter"}
    }
