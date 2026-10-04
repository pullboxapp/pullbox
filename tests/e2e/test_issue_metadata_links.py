"""Issue metadata review uses the existing modal, paging and stable-page contracts."""

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
def test_issue_field_sources_distinguish_passive_release_context(
    authed_page, seeded_server, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    provider_fields = [
        ("title", "comicvine_local", "ComicVine Local"),
        ("description", "comicvine_api", "ComicVine API"),
        ("cover_date", "metron_api", "Metron"),
        ("page_count", "gcd_local", "GCD Local"),
        ("language", "gcd_api_v2", "GCD API v2"),
    ]
    origins = [
        {"field": field, "source": source, "user_override": False, "passive_release": None}
        for field, source, _label in provider_fields
    ]
    origins.extend(
        [
            {
                "field": "store_date",
                "source": None,
                "user_override": False,
                "passive_release": {
                    "locg_series_id": "77",
                    "release_ids": ["1001", "1002"],
                    "fetched_at": "2026-10-04T00:00:00Z",
                    "issue_identity": {
                        "namespace": "metron",
                        "entity_kind": "issue",
                        "external_id": "123",
                    },
                    "issue_number_text": "1",
                    "matched_store_date": "2026-09-30",
                    "match_kind": "exact_issue",
                },
            },
            {"field": "credits", "source": None, "user_override": True},
            {"field": "issue_number_text", "source": None, "user_override": False},
        ]
    )
    page.route(
        "**/api/v1/issues/1/metadata-links",
        lambda route: route.fulfill(
            json={
                "current": {"title": "Library title"},
                "identities": [],
                "sources": [],
                "can_refresh": False,
                "origins": origins,
            }
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    panel = page.get_by_test_id("issue-metadata-links")
    summary = panel.get_by_text("Field sources", exact=True)
    expect(summary).to_be_visible()
    expect(panel.locator("dl")).to_be_hidden()
    summary.focus()
    summary.press("Enter")
    expect(summary).to_be_focused()
    for field, label in [
        *((field, label) for field, _source, label in provider_fields),
        ("store_date", "LOCG release context"),
        ("credits", "Your edit"),
        ("issue_number_text", "Existing library metadata"),
    ]:
        row = panel.locator("dl > div").filter(
            has=page.locator("dt").get_by_text(field.replace("_", " "), exact=True)
        )
        expect(row.locator("dd")).to_have_text(label)
    assert_no_axe_violations(
        page,
        name=f"issue-field-sources-{theme}-{width}",
        include=['[data-testid="issue-metadata-links"]'],
    )
    summary.press("Enter")
    expect(panel.locator("dl")).to_be_hidden()
    expect(summary).to_be_focused()
    assert not errors


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("tron", 1280)])
@pytest.mark.parametrize("remote_page", [False, True])
def test_issue_provider_review_and_refresh_without_page_reload(
    authed_page, seeded_server, theme, width, remote_page
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    data = {
        "current": {
            "series_id": 1,
            "series_title": "Batman",
            "issue_number_text": "1",
            "title": "Library title",
            "cover_date": "2024-01-01",
        },
        "identities": [{"namespace": "gcd", "external_id": "500", "state": "verified"}],
        "sources": [{"source": "metron_api", "label": "Metron", "series_external_id": "42"}],
        "can_refresh": True,
        "origins": [{"field": "description", "source": "gcd_local", "user_override": False}],
    }
    page.route("**/api/v1/issues/1/metadata-links", lambda route: route.fulfill(json=data))
    results = [
        {
            "source": "metron_api",
            "identity_namespace": "metron",
            "external_id": str(i),
            "series_external_id": "42",
            "issue_number_text": str(i),
            "title": f"Provider issue {i}",
            "page_count": 32,
            "cover_date": "2024-01-01",
        }
        for i in range(1, 26)
    ]

    def candidates(route):
        provider_page = route.request.post_data_json["page"]
        rows = results
        if provider_page == 2:
            rows = [
                {**results[0], "external_id": str(i), "issue_number_text": str(i)}
                for i in range(26, 34)
            ]
        route.fulfill(
            json={
                "status": "ok",
                "data": {
                    "results": rows,
                    "next_page": 2 if remote_page and provider_page == 1 else None,
                    "total": 33 if remote_page else 25,
                },
            }
        )

    page.route("**/api/v1/issues/1/metadata-links/candidates", candidates)
    preview = {
        "current": data["current"],
        "candidate": results[0],
        "source_revision": 1,
        "review": {
            "event_id": 1,
            "fingerprint": "a" * 64,
            "review_revision": 1,
            "external_id": "1",
        },
    }
    page.route(
        "**/api/v1/issues/1/metadata-links/preview", lambda route: route.fulfill(json=preview)
    )

    def confirm(route):
        data["identities"].append({"namespace": "metron", "external_id": "1", "state": "verified"})
        data["sources"] = []
        route.fulfill(json={"event_id": 2, "replayed": False})

    page.route("**/api/v1/issues/1/metadata-links/confirm", confirm)
    page.route(
        "**/api/v1/issues/1/refresh-metadata",
        lambda route: route.fulfill(json={"issue_id": 1, "outcomes": []}),
    )
    page.route(
        "**/htmx/issues/1/metadata",
        lambda route: route.fulfill(
            content_type="text/html",
            body='<div id="issue-metadata-content"><p>Updated issue metadata</p></div>',
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/issues/1")
    page.evaluate("theme => applyTheme(theme)", theme)
    panel = page.get_by_test_id("issue-metadata-links")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="Link provider", exact=True).click()
    dialog = page.get_by_role("dialog", name="Link issue metadata provider")
    dialog.get_by_role("button", name="Browse provider issues").click()
    expect(dialog.get_by_role("button", name="Review issue 1", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Review issue 11", exact=True)).to_have_count(0)
    dialog.get_by_role("button", name="Next", exact=True).click()
    expect(dialog.get_by_role("button", name="Review issue 11", exact=True)).to_be_visible()
    if remote_page:
        dialog.get_by_role("button", name="Next", exact=True).click()
        expect(dialog.get_by_role("button", name="Review issue 25", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Next", exact=True).click()
        expect(dialog.get_by_text("Issues 26-33", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="Review issue 26", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="Next", exact=True)).to_be_disabled()
        dialog.get_by_role("button", name="Previous", exact=True).click()
        expect(dialog.get_by_role("button", name="Review issue 21", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="Review issue 25", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Previous", exact=True).click()
    dialog.get_by_role("button", name="Previous", exact=True).click()
    dialog.get_by_role("button", name="Review issue 1", exact=True).click()
    expect(dialog.get_by_text("Confirm this is the same issue")).to_be_focused()
    assert_no_axe_violations(
        page,
        name=f"issue-provider-review-{theme}-{width}",
        include=['[aria-labelledby="issue-link-title"]'],
    )
    dialog.get_by_role("button", name="Link this issue").click()
    expect(dialog).to_be_hidden()
    expect(panel.get_by_text("Metron 1", exact=False)).to_be_visible()
    panel.evaluate("el => el.dataset.preserved = 'yes'")
    panel.get_by_role("button", name="Refresh metadata", exact=True).click()
    expect(page.get_by_text("Updated issue metadata", exact=True)).to_be_visible()
    expect(panel).to_have_attribute("data-preserved", "yes")
    expect(panel.get_by_role("button", name="Refresh metadata", exact=True)).to_be_enabled()
    panel.get_by_text("Field sources", exact=True).click()
    expect(panel.get_by_text("description", exact=True)).to_be_visible()
    assert not errors
