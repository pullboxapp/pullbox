"""The issue editor is available without a conflict and uses the standard modal."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_editor_select_preview_apply_and_validation_retention(
    authed_page, seeded_server, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 740})
    job = {"job": None}
    page.route("**/api/v1/issues/1/file-metadata/job", lambda route: route.fulfill(json=job))
    writes = []
    previews = []
    fields = [
        {
            "key": "xml.Pages",
            "label": "ComicInfo page annotations",
            "current": "2 page entries",
            "editable": False,
            "differs": False,
        },
        {
            "key": "issue.web_links",
            "label": "Reference links",
            "editable": True,
            "input_type": "links",
            "xml_conflict_key": "xml.Web",
            "options": [],
            "link_draft": [{"url": "https://publisher.example/edition", "primary": True}],
            "differs": False,
        },
        {
            "key": "issue.upc",
            "label": "UPC",
            "editable": True,
            "input_type": "text",
            "xml_conflict_key": "xml.UPC",
            "options": [{"source": "MetronInfo.xml", "value": "036000291452-00311"}],
            "differs": False,
        },
        {
            "key": "issue.gtin",
            "label": "ComicInfo barcode (GTIN)",
            "editable": True,
            "input_type": "text",
            "xml_conflict_key": "xml.GTIN",
            "options": [],
            "differs": False,
        },
        {
            "key": "issue.prices",
            "label": "Cover prices",
            "editable": True,
            "input_type": "prices",
            "xml_conflict_key": "xml.Prices",
            "price_draft": [{"country": "US", "amount": "2.00"}, {"country": "CA", "amount": "0"}],
            "options": [{"source": "MetronInfo.xml", "value": "US 2.00\nCA 0"}],
            "differs": False,
        },
        {
            "key": "issue.story_titles",
            "label": "Story titles",
            "input_type": "textarea",
            "editable": True,
            "xml_conflict_key": "xml.Stories",
            "options": [{"source": "MetronInfo.xml", "value": "Opening, Part I\nBackup story"}],
            "differs": False,
        },
        {
            "key": "issue.universes",
            "label": "Universes",
            "editable": True,
            "input_type": "universes",
            "xml_conflict_key": "xml.Universes",
            "universe_draft": [
                {"name": "Earth", "designation": "616"},
                {"name": "Earth", "designation": "1610"},
            ],
            "options": [{"source": "MetronInfo.xml", "value": "Earth (616)\nEarth (1610)"}],
            "differs": False,
        },
        {
            "key": "issue.series_groups",
            "label": "Series groups",
            "editable": True,
            "input_type": "textarea",
            "xml_conflict_key": "xml.SeriesGroup",
            "options": [{"source": "ComicInfo.xml", "value": "Gotham\nClassics"}],
            "differs": False,
        },
        {
            "key": "issue.manga",
            "label": "Manga",
            "editable": True,
            "input_type": "text",
            "xml_conflict_key": "xml.Manga",
            "options": [{"source": "ComicInfo.xml", "value": "Yes"}],
            "allowed_values": ["Unknown", "No", "Yes", "YesAndRightToLeft"],
            "differs": False,
        },
        {
            "key": "issue.community_rating",
            "label": "Community rating",
            "xml_conflict_key": "xml.CommunityRating",
            "current": "",
            "editable": True,
            "input_type": "text",
            "options": [{"source": "MetronInfo.xml", "value": "4.5"}],
            "reason": "MetronInfo records 12 votes for this average.",
            "differs": False,
        },
        {
            "key": "issue.notes",
            "label": "Notes",
            "xml_conflict_key": "xml.Notes",
            "current": "",
            "editable": True,
            "input_type": "textarea",
            "options": [{"source": "ComicInfo.xml", "value": "Scan note"}],
            "differs": False,
        },
        {
            "key": "issue.title",
            "label": "Title",
            "current": "Original",
            "proposed": "Original",
            "editable": True,
            "input_type": "text",
            "options": [{"source": "library", "value": "Original"}],
            "overridden": False,
            "differs": False,
            "selected": False,
            "origin": "metron_api",
            "current_source_label": "Metron",
        },
        {
            "key": "issue.description",
            "label": "Summary",
            "current": "Keep this",
            "proposed": "Keep this",
            "editable": True,
            "input_type": "textarea",
            "options": [{"source": "library", "value": "Keep this"}],
            "overridden": True,
            "differs": False,
            "selected": False,
            "origin": "Manual correction",
        },
        {
            "key": "issue.age_rating",
            "label": "Age rating",
            "current": "Everyone",
            "editable": True,
            "input_type": "text",
            "options": [
                {"source": "library", "value": "Everyone"},
                {"source": "ComicInfo.xml", "value": "PG", "selectable": False},
            ],
            "allowed_values": ["Unknown", "Everyone", "Teen"],
            "differs": False,
        },
        {
            "key": "issue.tags",
            "xml_conflict_key": "xml.Tags",
            "label": "Tags",
            "current": "",
            "editable": True,
            "input_type": "textarea",
            "options": [
                {"source": "library", "value": ""},
                {"source": "ComicInfo.xml", "value": "City\nNight"},
                {"source": "MetronInfo.xml", "value": "City\nNight"},
            ],
            "differs": False,
        },
        {
            "key": "series.title",
            "label": "Series title",
            "current": "Batman",
            "proposed": "Batman",
            "editable": False,
            "reason": "Series-wide field is protected.",
            "options": [],
            "differs": False,
            "selected": False,
        },
        {
            "key": "issue.imprint",
            "xml_conflict_key": "xml.Imprint",
            "label": "Imprint",
            "current": "",
            "editable": True,
            "input_type": "text",
            "options": [
                {"source": "library", "value": ""},
                {"source": "ComicInfo.xml", "value": "Harbor Books"},
                {"source": "MetronInfo.xml", "value": "Harbor Books"},
            ],
            "differs": False,
            "selected": False,
        },
        {
            "key": "issue.story_arcs",
            "label": "Embedded story arcs",
            "editable": True,
            "input_type": "arcs",
            "xml_conflict_key": "xml.StoryArc",
            "current": "",
            "arc_draft": [{"name": "Old arc", "position": 2}, {"name": "Keep", "position": 5}],
            "options": [{"source": "library", "value": ""}],
            "differs": False,
        },
    ]

    def preview(route):
        body = route.request.post_data_json
        previews.append(body)
        edits = body.get("edits", {})
        if edits.get("title", {}).get("value") == "Rejected":
            route.fulfill(status=409, json={"detail": "The file changed. Preview again."})
            return
        route.fulfill(
            json={
                "file_id": 1,
                "file_name": "Batman 001.cbz",
                "review_key": "a" * 64,
                "ready": True,
                "unchanged": False,
                "conflicts": [],
                "documents": ["ComicInfo.xml", "MetronInfo.xml"],
                "editor_fields": fields,
                "editor_changed": bool(edits),
                "changes": [
                    {
                        "document": "ComicInfo.xml",
                        "field": "Title",
                        "before": "Original",
                        "after": edits.get("title", {}).get("value", "Original"),
                    }
                ]
                if edits
                else [],
            }
        )

    def write(route):
        writes.append(route.request.post_data_json)
        job["job"] = {
            "id": "editor-test",
            "state": "RUNNING",
            "percent": 42,
            "message": "Verifying preserved comic pages",
            "error": None,
        }
        route.fulfill(status=202, json={"job_id": "editor-test", "state": "RUNNING"})

    page.route("**/api/v1/issues/1/metadata-editor/preview", preview)
    page.route("**/api/v1/issues/1/metadata-editor/write", write)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    trigger = page.get_by_role("button", name="Edit metadata", exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Edit issue metadata", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("No differences to review.")).to_be_visible()
    pages = dialog.get_by_role("group", name="ComicInfo page annotations", exact=True)
    expect(pages).to_have_count(0)
    dialog.get_by_role("button", name="All fields", exact=True).click()
    expect(pages.get_by_text("Embedded: 2 page entries", exact=True)).to_be_visible()
    expect(pages.get_by_role("checkbox")).to_have_count(0)
    expect(dialog.get_by_text("Saved source: Metron", exact=True)).to_be_visible()
    expect(dialog.get_by_text("metron_api", exact=True)).not_to_be_visible()
    summary = dialog.get_by_role("group", name="Summary", exact=True)
    expect(summary.get_by_text("Manual correction", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Series-wide field is protected.")).to_be_visible()
    dialog.get_by_role("button", name="Select all eligible", exact=True).click()
    expect(dialog.get_by_label("Update Title", exact=True)).to_be_checked()
    expect(dialog.get_by_label("Update Summary", exact=True)).to_be_checked()
    with page.expect_response("**/metadata-editor/preview"):
        dialog.get_by_role("button", name="Update preview", exact=True).click()
    assert previews[-1]["edits"] == {
        key: {"source": "library"} for key in ("title", "description", "age_rating")
    }
    expect(dialog.get_by_role("button", name="Value source for Tags")).to_contain_text(
        "Leave unchanged"
    )
    assert not writes
    dialog.get_by_role("button", name="Deselect all", exact=True).click()
    dialog.get_by_label("Update Title", exact=True).check()
    dialog.get_by_role("button", name="Value source for Title", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    dialog.get_by_label("New Title", exact=True).fill("Rejected")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("file changed")
    expect(dialog.get_by_label("New Title", exact=True)).to_have_value("Rejected")
    dialog.get_by_label("New Title", exact=True).fill("My corrected title")
    dialog.get_by_label("Update Age rating", exact=True).check()
    expect(
        dialog.get_by_text("ComicInfo.xml: PG (no exact equivalent)", exact=True)
    ).to_be_visible()
    dialog.get_by_role("button", name="Value source for Age rating", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    dialog.get_by_role("button", name="New Age rating", exact=True).click()
    page.get_by_role("option", name="Teen", exact=True).click()
    dialog.get_by_label("Update Manga", exact=True).check()
    dialog.get_by_role("button", name="Value source for Manga", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    dialog.get_by_role("button", name="New Manga", exact=True).click()
    page.get_by_role("option", name="Yes (right to left)", exact=True).click(timeout=4000)
    dialog.get_by_label("Update Tags", exact=True).check()
    dialog.get_by_role("button", name="Value source for Tags", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    tags = dialog.get_by_label("New Tags", exact=True)
    expect(tags).to_have_value("City\nNight")
    tags.fill("City\nHarbor")
    dialog.get_by_label("Update Series groups", exact=True).check()
    dialog.get_by_role("button", name="Value source for Series groups", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    groups = dialog.get_by_label("New Series groups", exact=True)
    expect(groups).to_have_value("Gotham\nClassics")
    groups.fill("Classics\nGotham & Beyond")
    dialog.get_by_label("Update ComicInfo barcode (GTIN)", exact=True).check()
    dialog.get_by_role(
        "button", name="Value source for ComicInfo barcode (GTIN)", exact=True
    ).click()
    expect(page.get_by_role("option", name="Use Metron ISBN", exact=True)).to_have_count(0)
    page.get_by_role("option", name="Use Metron UPC", exact=True).click()
    dialog.get_by_label("Update Cover prices", exact=True).check()
    dialog.get_by_label("Update Reference links", exact=True).check()
    dialog.get_by_role("button", name="Value source for Reference links", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    expect(dialog.get_by_label("Primary link 1", exact=True)).to_be_checked()
    dialog.get_by_role("button", name="Add link entry", exact=True).click()
    dialog.get_by_label("Reference URL 2", exact=True).fill("https://writer.example/interview")
    dialog.get_by_label("Primary link 2", exact=True).check()
    expect(dialog.get_by_label("Primary link 1", exact=True)).not_to_be_checked()
    dialog.get_by_role("button", name="Move link 2 up", exact=True).click()
    expect(dialog.get_by_label("Primary link 1", exact=True)).to_be_checked()
    dialog.get_by_role("button", name="Value source for Cover prices", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    expect(dialog.get_by_label("Price amount 2", exact=True)).to_have_value("0", timeout=2000)
    dialog.get_by_role("button", name="Move price 2 up", exact=True).click()
    expect(dialog.get_by_label("Price country 1", exact=True)).to_have_value("CA")
    dialog.get_by_label("Price amount 2", exact=True).fill("4.990")
    dialog.get_by_role("button", name="Add price entry", exact=True).click()
    dialog.get_by_label("Price country 3", exact=True).fill("gb")
    dialog.get_by_label("Price amount 3", exact=True).fill("1e5")
    before_bad_price = len(previews)
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("Enter each amount as a decimal")
    assert len(previews) == before_bad_price
    dialog.get_by_label("Price amount 3", exact=True).fill("0.123456789012345678")
    dialog.get_by_label("Update Story titles", exact=True).check()
    dialog.get_by_role("button", name="Value source for Story titles", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    stories = dialog.get_by_label("New Story titles", exact=True)
    expect(stories).to_have_value("Opening, Part I\nBackup story")
    stories.fill("Backup story\nCorrected opening")
    dialog.get_by_label("Update Universes", exact=True).check()
    dialog.get_by_role("button", name="Value source for Universes", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    expect(dialog.get_by_label("Universe designation 1", exact=True)).to_have_value("616")
    dialog.get_by_role("button", name="Move universe 2 up", exact=True).click()
    expect(dialog.get_by_label("Universe designation 1", exact=True)).to_have_value("1610")
    dialog.get_by_role("button", name="Remove universe 2", exact=True).click()
    dialog.get_by_role("button", name="Add universe entry", exact=True).click()
    dialog.get_by_label("Universe name 2", exact=True).fill("Elsewhere, too")
    dialog.get_by_label("Update Community rating", exact=True).check()
    expect(dialog.get_by_text("MetronInfo records 12 votes for this average.")).to_be_visible()
    dialog.get_by_role("button", name="Value source for Community rating", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    score = dialog.get_by_label("New Community rating", exact=True)
    expect(score).to_have_attribute("inputmode", "decimal")
    score.fill("0")
    dialog.get_by_label("Update Notes", exact=True).check()
    dialog.get_by_role("button", name="Value source for Notes", exact=True).click()
    page.get_by_role("option", name="ComicInfo.xml", exact=False).click()
    dialog.get_by_label("Update Imprint", exact=True).check()
    dialog.get_by_role("button", name="Value source for Imprint", exact=True).click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    imprint = dialog.get_by_label("New Imprint", exact=True)
    expect(imprint).to_have_value("Harbor Books")
    imprint.fill("Harbor Noir")
    dialog.get_by_label("Update Embedded story arcs", exact=True).check()
    dialog.get_by_role("button", name="Value source for Embedded story arcs").click()
    page.get_by_role("option", name="Enter correction", exact=True).click()
    expect(dialog.get_by_label("Arc name 1", exact=True)).to_have_value("Old arc")
    dialog.get_by_role("button", name="Remove arc 1", exact=True).click()
    expect(dialog.get_by_label("Arc name 1", exact=True)).to_have_value("Keep")
    dialog.get_by_label("Arc position 1", exact=True).fill("6")
    dialog.get_by_role("button", name="Add arc entry").click()
    before_invalid = len(previews)
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("Each arc needs a name")
    assert len(previews) == before_invalid
    expect(dialog.get_by_label("Arc position 1", exact=True)).to_have_value("6")
    dialog.get_by_label("Arc name 2", exact=True).fill("New | Arc")
    dialog.get_by_role("button", name="Update preview", exact=True).click()
    expect(dialog.get_by_role("button", name="Apply selected changes", exact=True)).to_be_enabled()
    selected = {
        "web_links": {
            "source": "custom",
            "value": [
                {"url": "https://writer.example/interview", "primary": True},
                {"url": "https://publisher.example/edition", "primary": False},
            ],
        },
        "gtin": {"source": "upc"},
        "prices": {
            "source": "custom",
            "value": [
                {"country": "CA", "amount": "0"},
                {"country": "US", "amount": "4.990"},
                {"country": "GB", "amount": "0.123456789012345678"},
            ],
        },
        "story_titles": {"source": "custom", "value": "Backup story\nCorrected opening"},
        "universes": {
            "source": "custom",
            "value": [
                {"name": "Earth", "designation": "1610"},
                {"name": "Elsewhere, too", "designation": None},
            ],
        },
        "series_groups": {"source": "custom", "value": "Classics\nGotham & Beyond"},
        "manga": {"source": "custom", "value": "YesAndRightToLeft"},
        "community_rating": {"source": "custom", "value": "0"},
        "notes": {"source": "ComicInfo.xml"},
        "title": {"source": "custom", "value": "My corrected title"},
        "age_rating": {"source": "custom", "value": "Teen"},
        "tags": {"source": "custom", "value": "City\nHarbor"},
        "imprint": {"source": "custom", "value": "Harbor Noir"},
        "story_arcs": {
            "source": "custom",
            "value": [{"name": "Keep", "position": 6}, {"name": "New | Arc", "position": None}],
        },
    }
    assert previews[-1] == {"edits": selected}
    assert_no_axe_violations(
        page, name=f"issue-editor-{theme}", include=['[aria-labelledby="file-metadata-title"]']
    )
    bounds = dialog.bounding_box()
    assert bounds and bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 740
    page.screenshot(
        path=str(Path(__file__).resolve().parents[2] / f"test-results/issue-editor/{theme}.png")
    )
    dialog.get_by_role("button", name="Apply selected changes", exact=True).click()
    expect(dialog.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "42")
    assert writes == [
        {
            "edits": selected,
            "review_key": "a" * 64,
        }
    ]
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(trigger).to_be_focused()
    assert not errors
