"""GCD native arc search, reviewed adoption and refresh through real commands."""

import httpx
import pytest
from sqlalchemy import func, select, update

from pullbox.config import get_settings
from pullbox.models import Issue, Series, StoryArc, StoryArcExternalIdentity
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.story_arc import IssueStoryArc
from tests.api.test_metadata_sources_api import csrf, policy
from tests.api.test_source_arc_catalog_api import BASE
from tests.api.test_source_arc_catalog_api import sec_db as _sec_db
from tests.ui.test_source_story_arc_ui import post, seed
from tests.unit.test_gcd_api_arcs import arc_row, envelope
from tests.unit.test_gcd_api_v2 import TOKEN, issue_row, series_row, source

pytest_plugins = ["conftest_security"]
sec_db = _sec_db
PREVIEW = "/story-arcs/catalog/gcd_api_v2/4"


@pytest.fixture
async def gcd_arc_setup(authenticated_client, sec_app, sec_db, monkeypatch, tmp_path):
    from pullbox.api.deps import get_settings_dep

    settings = get_settings().model_copy(update={"metadata_gcd_api_v2_enabled": True})
    sec_app.dependency_overrides[get_settings_dep] = lambda: settings
    for module in ("metadata_arc_search", "story_arc_source_routes"):
        monkeypatch.setattr(f"pullbox.ui.{module}.get_settings", lambda: settings)
    saved = await authenticated_client.put(
        "/api/v1/metadata/sources/gcd_api_v2",
        json=policy(credential=TOKEN),
        headers=csrf(authenticated_client),
    )
    assert saved.status_code == 200, saved.text
    root_path = tmp_path / "comics"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Managed", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        root_id = root.id
    fixture = {
        "selection": {"source": "gcd_api_v2", "external_id": "4", "source_revision": 1},
        "root_id": root_id,
        "root": root_path,
        "calls": [],
        "issues": [issue_row(), issue_row(765610, "50-x")],
        "failure": None,
        "bad_parent": False,
        "broken_page": False,
    }

    def handle(request):
        fixture["calls"].append(request)
        if fixture["failure"] is not None:
            return httpx.Response(fixture["failure"], headers={"Retry-After": "30"})
        if request.url.path == "/api/v2/story-arcs/":
            return httpx.Response(200, json=envelope(request, [arc_row()]))
        if request.url.path == "/api/v2/story-arcs/4/":
            return httpx.Response(200, json=arc_row())
        if request.url.path == "/api/v2/story-arcs/4/issues/":
            data = envelope(request, fixture["issues"])
            if fixture["broken_page"] and request.url.params["page"] == "2":
                data["results"] = data["results"][:-1]
            return httpx.Response(200, json=data)
        assert request.url.path == "/api/v2/series/50494/", "No issue-by-issue catalog fetch"
        return httpx.Response(200, json=series_row(99 if fixture["bad_parent"] else 50494))

    monkeypatch.setattr(
        "pullbox.providers.metadata.sources.GcdApiV2Source", lambda _: source(handle)
    )
    return fixture


async def preview(client, fixture):
    response = await client.post(BASE + "/preview", json=fixture["selection"], headers=csrf(client))
    assert response.status_code == 200, response.text
    return response.json()


def decision(fixture, snapshot):
    return {
        **fixture["selection"],
        "fingerprint": snapshot["fingerprint"],
        "file_defaults_fingerprint": snapshot["file_defaults_fingerprint"],
        "library_root_id": fixture["root_id"],
        "ordered_issue_ids": [row["external_id"] for row in reversed(snapshot["issues"])],
        "skipped_issue_ids": [snapshot["issues"][0]["external_id"]],
    }


async def test_search_review_add_repeat_and_refresh_preserve_native_graph_and_decisions(
    authenticated_client, sec_db, gcd_arc_setup
):
    client, fixture = authenticated_client, gcd_arc_setup
    search = await client.get("/story-arcs/add?q=Civil+War&source=gcd_api_v2")
    assert search.status_code == 200 and f'href="{PREVIEW}"' in search.text
    data = seed((await client.get(PREVIEW)).text)
    assert data["ready"] and data["sourceLabel"] == "GCD API"
    assert [row["issue_number"] for row in data["members"]] == ["1", "50-x"]
    added = await post(
        client,
        PREVIEW,
        [
            ("source_revision", str(data["sourceRevision"])),
            ("fingerprint", data["fingerprint"]),
            ("file_defaults_fingerprint", data["fileDefaultsFingerprint"]),
            ("library_root_id", str(fixture["root_id"])),
            ("issue_provider_ids", "765610"),
            ("issue_provider_ids", "765609"),
            ("reading_orders", "1"),
            ("reading_orders", "2"),
            ("skipped_issue_provider_ids", "765609"),
        ],
    )
    assert added.status_code == 303 and "catalog-added" in added.headers["location"]
    arc_id = int(added.headers["location"].split("/")[-1].split("?")[0])
    repeat = await client.get(PREVIEW, follow_redirects=False)
    assert repeat.status_code == 303 and repeat.headers["location"] == f"/story-arcs/{arc_id}"
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == 1
        assert (await session.scalar(select(StoryArc))).comicvine_id is None
        identity = await session.scalar(select(StoryArcExternalIdentity))
        assert identity.source == "gcd" and identity.external_id == "4"
        assert all(issue.comicvine_id is None for issue in await session.scalars(select(Issue)))
    fixture["issues"] = [issue_row(765610, "50-x"), issue_row(765611, "50-o")]
    refresh_ui = await client.get(f"/story-arcs/{arc_id}/catalog-refresh")
    assert refresh_ui.status_code == 200 and "Compare GCD API" in refresh_ui.text
    response = await client.post(
        f"{BASE}/{arc_id}/preview", json=fixture["selection"], headers=csrf(client)
    )
    snapshot = response.json()
    assert snapshot["changes"]["added_issue_ids"] == ["765611"]
    assert snapshot["changes"]["removed_issue_ids"] == ["765609"]
    refresh = {
        **fixture["selection"],
        "fingerprint": snapshot["fingerprint"],
        "expected_revision": snapshot["changes"]["revision"],
    }
    result = await client.post(f"{BASE}/{arc_id}", json=refresh, headers=csrf(client))
    assert result.status_code == 200 and result.json()["membership_count"] == 3
    async with sec_db() as session:
        members = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [row.source_issue_id for row in members] == ["765610", "765609", "765611"]
        assert members[1].resolution_state.value == "skipped"
        assert members[2].evidence["catalog_review_required"] and not members[2].sync_eligible
    assert not list(fixture["root"].iterdir())
    assert all("If-None-Match" not in call.headers for call in fixture["calls"])


async def test_complete_paginated_arc_add_uses_pages_and_one_parent_read(
    authenticated_client, gcd_arc_setup
):
    client, fixture = authenticated_client, gcd_arc_setup
    fixture["issues"] = [issue_row(1000 + i, str(i + 1)) for i in range(103)]
    snapshot = await preview(client, fixture)
    assert len(snapshot["issues"]) == 103 and len(fixture["calls"]) == 4
    response = await client.post(BASE, json=decision(fixture, snapshot), headers=csrf(client))
    assert response.status_code == 201 and response.json()["membership_count"] == 103
    assert len(fixture["calls"]) == 8


@pytest.mark.parametrize(
    "change",
    [
        "membership",
        "partial_page",
        "repeated_page_id",
        "parent",
        "policy",
        "disabled",
        "variant",
        "404",
        "429",
    ],
)
async def test_failed_revalidation_leaves_database_and_files_untouched(
    authenticated_client, sec_db, gcd_arc_setup, change
):
    client, fixture = authenticated_client, gcd_arc_setup
    body = decision(fixture, await preview(client, fixture))
    fixture["calls"].clear()
    if change == "membership":
        fixture["issues"] = [issue_row(765610, "50-x")]
    elif change in {"partial_page", "repeated_page_id"}:
        fixture["issues"] = [issue_row(1000 + i, str(i + 1)) for i in range(103)]
        fixture["broken_page"] = change == "partial_page"
        if change == "repeated_page_id":
            fixture["issues"][-1] = fixture["issues"][0]
    elif change == "parent":
        fixture["bad_parent"] = True
    elif change in {"policy", "disabled"}:
        async with sec_db.begin() as session:
            await session.execute(
                update(MetadataSourceConfig).values(revision=2, enabled=change != "disabled")
            )
    elif change == "variant":
        fixture["issues"][0]["variant_of"] = 765610
    else:
        fixture["failure"] = int(change)
    result = await client.post(BASE, json=body, headers=csrf(client))
    assert result.status_code == 409, result.text
    if change in {"policy", "disabled"}:
        assert not fixture["calls"]
    if change == "429":
        assert result.headers["Retry-After"] == "30"
    async with sec_db() as session:
        for model in (StoryArc, Series, Issue):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert not list(fixture["root"].iterdir()) and TOKEN not in result.text
