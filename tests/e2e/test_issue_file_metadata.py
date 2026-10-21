"""Existing-file metadata approval uses the stable modal and progress contracts."""

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


def test_partial_queue_dismiss_confirmation(authed_page, seeded_server):
    from pullbox.database import get_session_factory
    from pullbox.models.pending_match import PendingMatch
    from tests.e2e.conftest import _run_async_blocking

    async def seed():
        async with get_session_factory()() as session:
            pending = PendingMatch(
                issue_id=1,
                release_title="Batman 001 (B Story).cbz",
                download_url="pullbox-direct://attempt/100",
                confidence="high",
                match_details={
                    "source_kind": "direct",
                    "failure_class": "post_process",
                    "failure_code": "direct_metadata_review_required",
                },
            )
            session.add(pending)
            await session.commit()
            return pending.id

    pending_id = _run_async_blocking(seed())

    async def cleanup():
        async with get_session_factory()() as session:
            pending = await session.get(PendingMatch, pending_id)
            if pending:
                await session.delete(pending)
                await session.commit()

    page = authed_page
    dismissed = []
    page.route(
        f"**/htmx/intervention/{pending_id}/reject",
        lambda route: (dismissed.append(route.request.method), route.fulfill(status=204)),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{seeded_server}/intervention?tab=recovery")
        trigger = page.get_by_test_id(f"intervention-reject-{pending_id}")
        expect(page.get_by_role("link", name="Find complete copy", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Import replacement", exact=True)).to_be_visible()
        trigger.click()
        confirm = page.get_by_role("dialog", name="Dismiss recovery?", exact=True)
        confirm.get_by_role("button", name="Cancel", exact=True).click()
        assert dismissed == []
        trigger.click()
        confirm.get_by_role("button", name="Dismiss recovery", exact=True).click()
        expect(confirm).not_to_be_visible()
        page.wait_for_function("() => !document.querySelector('.htmx-request')")
        assert dismissed == ["POST"]
        assert not errors
    finally:
        _run_async_blocking(cleanup())


def test_replacement_download_requires_confirmation_before_grab(authed_page, seeded_server):
    page = authed_page
    page.route(
        "**/htmx/issues/1/search-results*",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""<div x-data="issueSearchResultActions({issueId: 1, hasLibraryFile: true})">
            <button class="btn btn-primary" @click="grabRelease($el)" :disabled="grabbing"
                    data-direct-attempt="42">Download complete copy</button></div>""",
        ),
    )
    grabs = []
    page.route(
        "**/api/v1/issues/1/direct-grab",
        lambda route: (
            grabs.append(route.request.post_data_json),
            route.fulfill(json={"queued": True}),
        ),
    )
    page.goto(f"{seeded_server}/issues/1?replacement=search")
    search = page.get_by_test_id("issue-search-modal")
    trigger = search.get_by_role("button", name="Download complete copy", exact=True)
    trigger.click()
    confirm = page.get_by_role("dialog", name="Download replacement?", exact=True)
    expect(
        confirm.get_by_text("stays in place until replacement succeeds", exact=False)
    ).to_be_visible()
    assert grabs == []
    confirm.get_by_role("button", name="Cancel", exact=True).click()
    expect(search).to_be_visible()
    expect(trigger).to_be_enabled()
    assert grabs == []
    trigger.click()
    confirm.get_by_role("button", name="Download replacement", exact=True).click()
    expect(trigger).to_be_enabled()
    assert grabs == [{"direct_attempt_id": 42}]


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
@pytest.mark.parametrize("action", ["search", "import"])
def test_partial_recovery_links_open_existing_replacement_workflows(
    authed_page, seeded_server, theme, width, action
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 650})
    endpoint = "**/api/v1/intervention/100/file-metadata"
    page.route(endpoint + "/job", lambda route: route.fulfill(json={"job": None}))
    page.route(
        endpoint + "/preview",
        lambda route: route.fulfill(
            json={
                "issue_id": 1,
                "file_name": "MCP 158 (B Story).cbz",
                "review_key": "0" * 64,
                "ready": False,
                "replacement_required": True,
                "conflicts": [],
                "changes": [],
            }
        ),
    )
    page.route(
        "**/htmx/issues/1/search-results*",
        lambda route: route.fulfill(
            body="<p>Complete copy candidates</p>", content_type="text/html"
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/intervention?tab=recovery")
    page.evaluate("theme => applyTheme(theme)", theme)
    page.evaluate(
        "window.dispatchEvent(new CustomEvent('review-direct-metadata', {detail: {pendingId: 100}}))"
    )
    dialog = page.get_by_role("dialog", name="Recover downloaded file", exact=True)
    expect(dialog.get_by_text("Incomplete copy: replacement needed", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Finish recovery", exact=True)).not_to_be_visible()
    expect(dialog.get_by_role("button", name="Preview again", exact=True)).not_to_be_visible()
    assert_no_axe_violations(
        page,
        name=f"partial-recovery-{theme}-{action}",
        include=['[aria-labelledby="file-metadata-title"]'],
    )
    dialog.get_by_role(
        "link",
        name="Find complete copy" if action == "search" else "Import replacement",
        exact=True,
    ).click()
    if action == "search":
        expect(page.get_by_test_id("issue-search-modal")).to_be_visible()
        expect(page.get_by_text("Complete copy candidates", exact=True)).to_be_visible()
    else:
        expect(page.get_by_test_id("file-browser-title")).to_have_text("Select Comic File")
        expect(page.get_by_test_id("file-browser-title")).to_be_visible()
    assert "replacement=" not in page.url
    assert not errors


def test_partial_recovery_dismiss_requires_confirmation_without_repairing(
    authed_page, seeded_server
):
    page = authed_page
    endpoint = "**/api/v1/intervention/100/file-metadata"
    page.route(endpoint + "/job", lambda route: route.fulfill(json={"job": None}))
    page.route(
        endpoint + "/preview",
        lambda route: route.fulfill(
            json={
                "issue_id": 1,
                "file_name": "MCP 158 (B Story).cbz",
                "review_key": "0" * 64,
                "ready": False,
                "replacement_required": True,
                "conflicts": [],
                "changes": [],
            }
        ),
    )
    dismissed = []
    page.route(
        endpoint + "/dismiss",
        lambda route: (
            dismissed.append(route.request.method),
            route.fulfill(json={"dismissed": True}),
        ),
    )
    page.goto(f"{seeded_server}/intervention?tab=recovery")
    page.evaluate(
        "window.dispatchEvent(new CustomEvent('review-direct-metadata', {detail: {pendingId: 100}}))"
    )
    dialog = page.get_by_role("dialog", name="Recover downloaded file", exact=True)
    dialog.get_by_role("button", name="Dismiss recovery", exact=True).click()
    confirm = page.get_by_role("dialog", name="Dismiss recovery?", exact=True)
    expect(confirm.get_by_text("does not fix or delete", exact=False)).to_be_visible()
    confirm.get_by_role("button", name="Cancel", exact=True).click()
    assert not dismissed
    dialog.get_by_role("button", name="Dismiss recovery", exact=True).click()
    confirm.get_by_role("button", name="Dismiss recovery", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert dismissed == ["POST"]


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_recovery_review_scrolls_and_finishes_without_reacquisition(
    authed_page, seeded_server, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 600})
    endpoint = "**/api/v1/intervention/100/file-metadata"
    state = {"job": None, "resolved": False}
    page.route(endpoint + "/job", lambda route: route.fulfill(json=state))

    def preview(route):
        selected = route.request.post_data_json.get("choices", {}).get("series.issue_count")
        route.fulfill(
            json={
                "issue_id": 1,
                "file_name": "Nice House 002.cbz",
                "review_key": "a" * 64,
                "ready": bool(selected),
                "unchanged": bool(selected),
                "changes": [],
                "conflicts": [
                    {
                        "key": "series.issue_count",
                        "label": "Issue count",
                        "selected": selected,
                        "options": [
                            {"source": source, "value": value, "selectable": True}
                            for source, value in [("library", "12"), ("ComicInfo.xml", "8")]
                        ],
                    }
                ],
            }
        )

    page.route(endpoint + "/preview", preview)

    def write(route):
        assert route.request.post_data_json == {
            "review_key": "a" * 64,
            "choices": {"series.issue_count": "library"},
        }
        state.update(
            job={
                "id": "recovery",
                "state": "COMPLETED",
                "percent": 100,
                "message": "Complete",
                "error": None,
            },
            resolved=True,
        )
        route.fulfill(status=202, json={"job_id": "recovery", "state": "COMPLETED"})

    page.route(endpoint + "/write", write)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/intervention?tab=recovery")
    page.evaluate("theme => applyTheme(theme)", theme)
    page.evaluate(
        "window.dispatchEvent(new CustomEvent('review-direct-metadata', {detail: {pendingId: 100}}))"
    )
    dialog = page.get_by_role("dialog", name="Recover downloaded file", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Finish recovery")).to_be_disabled()
    dialog.get_by_role("radio", name="Library 12").check()
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Finish recovery")).to_be_enabled()
    box = dialog.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 600
    assert dialog.locator(".modal-body").evaluate("el => getComputedStyle(el).overflowY") == "auto"
    assert_no_axe_violations(
        page, name=f"recovery-metadata-{theme}", include=['[aria-labelledby="file-metadata-title"]']
    )
    dialog.get_by_role("button", name="Finish recovery").click()
    expect(
        dialog.get_by_text("Recovery complete. The queue entry has been resolved.")
    ).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert not errors


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_native_metadata_review_requires_explicit_conversion(
    authed_page, seeded_server, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    endpoint = "**/api/v1/issues/1/file-metadata"
    page.route(endpoint + "/job", lambda route: route.fulfill(json={"job": None}))
    page.route(
        endpoint + "/preview",
        lambda route: route.fulfill(
            json={
                "file_id": 1,
                "file_name": "Batman 001.cb7",
                "review_key": "a" * 64,
                "unchanged": False,
                "ready": True,
                "changes": [],
                "converts_to_cbz": True,
            }
        ),
    )
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_role("button", name="Write file metadata", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Write file metadata", exact=True)
    expect(
        dialog.get_by_text("This does not rename or move the file.", exact=False)
    ).not_to_be_visible()
    expect(dialog.get_by_text("Its original is saved", exact=False)).to_be_visible()
    expect(dialog.get_by_role("button", name="Convert to CBZ and write metadata")).to_be_enabled()
    expect(dialog.get_by_role("button", name="Write both metadata files")).not_to_be_visible()
    assert_no_axe_violations(
        page, name=f"native-metadata-{theme}", include=['[aria-labelledby="file-metadata-title"]']
    )
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(trigger).to_be_focused()


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_file_metadata_preview_write_progress_and_focus(authed_page, seeded_server, theme, width):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    state = {"job": None}
    endpoint = "**/api/v1/issues/1/file-metadata"
    page.route(endpoint + "/job", lambda route: route.fulfill(json=state))
    page.route(
        endpoint + "/preview",
        lambda route: route.fulfill(
            json={
                "file_id": 1,
                "file_name": "Batman 001.cbz",
                "review_key": "a" * 64,
                "unchanged": False,
                "documents": ["ComicInfo.xml", "MetronInfo.xml"],
                "changes": [
                    {
                        "document": "MetronInfo.xml",
                        "field": "Summary",
                        "before": None,
                        "after": "Preserved summary",
                    }
                ],
            }
        ),
    )

    def write(route):
        assert route.request.post_data_json == {"review_key": "a" * 64}
        state["job"] = {
            "id": "test",
            "state": "RUNNING",
            "percent": 42,
            "message": "Verifying preserved comic pages",
            "error": None,
        }
        route.fulfill(status=202, json={"job_id": "test", "state": "RUNNING"})

    page.route(endpoint + "/write", write)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    button = page.get_by_role("button", name="Write file metadata", exact=True)
    expect(button).to_be_visible()
    button.click()
    dialog = page.get_by_role("dialog", name="Write file metadata", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Preserved summary", exact=False)).to_be_visible()
    assert_no_axe_violations(
        page,
        name=f"file-metadata-{theme}-{width}",
        include=['[aria-labelledby="file-metadata-title"]'],
    )
    dialog.get_by_role("button", name="Write both metadata files").click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "42")
    expect(dialog.get_by_role("button", name="Cancel write")).to_be_visible()
    state["job"]["state"] = "COMPLETED"
    state["job"]["percent"] = 100
    expect(dialog.get_by_text("Both metadata files are up to date.")).to_be_visible()
    dialog.get_by_role("button", name="Preview again", exact=True).click()
    expect(dialog.get_by_role("button", name="Write both metadata files")).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(button).to_be_focused()
    assert not errors


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
@pytest.mark.parametrize(
    "field,label", [("issue.description", "Issue description"), ("xml.AgeRating", "Age rating")]
)
def test_conflicting_values_need_a_choice_and_updated_preview(
    authed_page, seeded_server, theme, width, field, label
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    endpoint = "**/api/v1/issues/1/file-metadata"
    state = {"job": None}
    choices = {field: "ComicInfo.xml"}
    requests = []
    page.route(endpoint + "/job", lambda route: route.fulfill(json=state))

    def preview(route):
        body = route.request.post_data_json
        requests.append(body)
        selected = body.get("choices", {}).get(field)
        route.fulfill(
            json={
                "file_id": 1,
                "file_name": "Batman 001.cbz",
                "review_key": ("b" if selected else "a") * 64,
                "unchanged": False,
                "ready": bool(selected),
                "changes": [],
                "conflicts": [
                    {
                        "key": field,
                        "label": label,
                        "selected": selected,
                        "note": "",
                        "options": [
                            {"source": source, "value": value, "selectable": True}
                            for source, value in [
                                ("library", "Library summary"),
                                ("ComicInfo.xml", "File summary"),
                                ("MetronInfo.xml", "Other summary"),
                            ]
                            if field != "xml.AgeRating" or source != "library"
                        ],
                    }
                ],
            }
        )

    def write(route):
        assert route.request.post_data_json == {"review_key": "b" * 64, "choices": choices}
        state["job"] = {
            "id": "choice",
            "state": "COMPLETED",
            "percent": 100,
            "message": "Complete",
            "error": None,
        }
        route.fulfill(status=202, json={"job_id": "choice", "state": "COMPLETED"})

    page.route(endpoint + "/preview", preview)
    page.route(endpoint + "/write", write)
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_role("button", name="Write file metadata", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Write file metadata", exact=True)
    expect(dialog.get_by_text(label, exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Write both metadata files")).to_be_disabled()
    expect(dialog.locator("input:checked")).to_have_count(0)
    dialog.get_by_role("radio", name="ComicInfo.xml File summary").check()
    assert requests == [{}], "Choosing is local; it must not write or replace the modal"
    expect(dialog.get_by_role("button", name="Write both metadata files")).to_be_disabled()
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Write both metadata files")).to_be_enabled()
    assert requests[-1] == {"choices": choices}
    assert_no_axe_violations(
        page,
        name=f"metadata-choices-{field}-{theme}",
        include=['[aria-labelledby="file-metadata-title"]'],
    )
    dialog.get_by_role("button", name="Write both metadata files").click()
    expect(dialog.get_by_text("Both metadata files are up to date.")).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(trigger).to_be_focused()
