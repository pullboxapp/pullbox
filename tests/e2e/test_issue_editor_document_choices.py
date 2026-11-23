"""A saved-document shortcut changes drafts only, never the scope or an archive."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("document", ["ComicInfo.xml", "MetronInfo.xml"])
def test_use_document_for_checked_fields_only(authed_page, seeded_server, document):
    page = authed_page
    previews, writes = [], []
    endpoint = "**/api/v1/issues/1/metadata-editor"

    def field(key, label, library, ci, mi):
        return {
            "key": "issue." + key,
            "label": label,
            "current": library,
            "editable": True,
            "input_type": "text",
            "differs": True,
            "options": [
                {"source": "library", "value": library},
                {"source": "ComicInfo.xml", **ci},
                *([{"source": "MetronInfo.xml", **mi}] if mi is not None else []),
            ],
        }

    fields = [
        field("title", "Title", "Library", {"value": "Printed"}, {"value": "Digital"}),
        field("page_count", "Page count", "", {"value": "0"}, {"value": "28"}),
        field("review", "Review", "My review", {"value": "Printed review"}, None),
        field(
            "age_rating",
            "Age rating",
            "Teen",
            {"value": "PG", "selectable": False},
            {"value": "   "},
        ),
        field(
            "description",
            "Summary",
            "Manual summary",
            {"value": "Summary A"},
            {"value": "Summary B"},
        ),
        {
            "key": "issue.issue_number_text",
            "label": "Issue number",
            "editable": False,
            "differs": True,
        },
    ]

    def preview(route):
        edits = route.request.post_data_json["edits"]
        previews.append(edits)
        route.fulfill(
            json={
                "file_id": 1,
                "file_name": "sample.cbz",
                "review_key": "a" * 64,
                "ready": True,
                "unchanged": False,
                "conflicts": [],
                "editor_fields": fields,
                "editor_changed": bool(edits),
                "changes": [],
            }
        )

    def write(route):
        writes.append(route.request.post_data_json)
        route.fulfill(status=409, json={"detail": "Unexpected write"})

    page.route(
        "**/api/v1/issues/1/file-metadata/job", lambda route: route.fulfill(json={"job": None})
    )
    page.route(endpoint + "/preview", preview)
    page.route(endpoint + "/write", write)
    page.goto(f"{seeded_server}/issues/1")
    page.get_by_role("button", name="Edit metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit issue metadata")
    shortcut = dialog.get_by_role("button", name="Use " + document, exact=True)
    expect(shortcut).to_be_hidden()
    for label in ("Title", "Page count", "Age rating", "Review"):
        dialog.get_by_role("checkbox", name="Update " + label, exact=True).check()
    dialog.get_by_role("button", name="Value source for Age rating").click()
    page.get_by_role("option", name="Clear value", exact=True).click()
    update = dialog.get_by_role("button", name="Update preview")
    apply = dialog.get_by_role("button", name="Apply selected changes")
    update.click()
    expect(apply).to_be_enabled()
    before = list(previews)
    expect(shortcut).to_be_enabled()
    shortcut.click()
    eligible = 3 if document == "ComicInfo.xml" else 2
    expect(
        dialog.get_by_text(f"{eligible} of 4 checked fields use {document}.", exact=True)
    ).to_be_visible()
    expect(apply).to_be_disabled()
    expect(dialog.get_by_role("checkbox", name="Update Summary")).not_to_be_checked()
    expect(dialog.get_by_role("checkbox", name="Update Issue number")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Value source for Age rating")).to_contain_text(
        "Clear value"
    )
    assert previews == before and not writes
    update.click()
    expect(apply).to_be_enabled()
    assert previews[-1] == {
        "title": {"source": document},
        "page_count": {"source": document},
        "age_rating": {"source": "clear"},
        "review": {"source": "ComicInfo.xml" if document == "ComicInfo.xml" else "library"},
    }
    expect(shortcut).to_be_disabled()
    dialog.get_by_role("button", name="Close", exact=True).click()
    assert not writes
