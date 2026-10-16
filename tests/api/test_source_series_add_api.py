"""Source-aware Add Series uses server metadata, real auth and committed side effects."""

import os
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from pullbox.core.events import EventBus, SeriesAdded
from pullbox.core.metadata_identity import MetadataSource
from pullbox.models import Issue, Series
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.schemas.metadata_sources import SourceCapability
from pullbox.services.metadata_discovery import SourceRegistration
from tests.api.test_metadata_sources_api import csrf, policy
from tests.unit.test_metron_source import envelope, issue_row, series_row, source

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
async def source_add_setup(authenticated_client, sec_db, monkeypatch, tmp_path):
    from pullbox.api.v1 import series as routes
    from pullbox.providers.metadata import sources

    saved = await authenticated_client.put(
        "/api/v1/metadata/sources/metron_api",
        json=policy(credential="synthetic-token"),
        headers=csrf(authenticated_client),
    )
    assert saved.status_code == 200
    root_path = tmp_path / "managed"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Test", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        session.add(SystemConfig(key="search_on_add_default", value="true"))
        await session.flush()
        root_id = root.id
    calls = []
    responses = []
    profile = {**series_row(8), "name": "Fixture 8", "cv_id": None, "gcd_id": None}

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/series/8/":
            return httpx.Response(200, json=profile)
        assert request.url.path == "/api/series/8/issue_list/"
        if responses:
            return responses.pop(0)
        return httpx.Response(
            200,
            json=envelope(
                [
                    issue_row(100, "13a"),
                    issue_row(101, "13b"),
                    issue_row(102, "50-x"),
                ]
            ),
        )

    original = sources.metadata_sources
    monkeypatch.setattr(
        sources,
        "metadata_sources",
        lambda: {
            **original(),
            MetadataSource.METRON_API: SourceRegistration(
                frozenset({SourceCapability.SERIES_DETAILS, SourceCapability.ISSUE_LIST}),
                lambda _: source(handle),
            ),
        },
    )
    events = []
    bus = EventBus()

    async def observe(event):
        async with sec_db() as session:
            found = await session.get(Series, event.series_id)
            assert found is not None and Path(found.path).is_dir()
        events.append(event)

    bus.subscribe(SeriesAdded, observe)
    monkeypatch.setattr(routes, "get_event_bus", lambda: bus)

    async def no_legacy(_session):
        pytest.fail("A source Add must not construct a ComicVine service")

    monkeypatch.setattr(routes, "_build_series_service", no_legacy)

    async def covers_dir(_session):
        return tmp_path / "covers"

    monkeypatch.setattr("pullbox.services.cover_cache_service.resolve_covers_dir", covers_dir)
    return {
        "body": {
            "source": "metron_api",
            "external_id": "8",
            "source_revision": 1,
            "library_root_id": root_id,
        },
        "calls": calls,
        "responses": responses,
        "profile": profile,
        "events": events,
        "root": root_path,
    }


async def test_add_native_series_uses_full_server_catalog_and_is_repeat_safe(
    authenticated_client,
    source_add_setup,
    sec_db,
):
    fixture = source_add_setup
    response = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["comicvine_id"] is None and data["title"] == "Fixture 8"
    assert data["issue_count"] == 3 and data["issue_catalog_state"] == "complete"
    assert data["wanted_count"] == 3 and data["monitored"] is True
    assert Path(data["path"]).is_dir() and len(fixture["events"]) == 1
    again = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert again.status_code == 201 and again.json()["id"] == data["id"]
    assert len(fixture["events"]) == 1
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 3


@pytest.mark.parametrize("invalid_after_preview", [False, True])
async def test_stale_summary_preview_and_final_add_revalidate_the_catalog(
    authenticated_client, source_add_setup, sec_db, invalid_after_preview
):
    fixture = source_add_setup
    fixture["profile"]["issue_count"] = 1
    complete = envelope([issue_row(100, "1"), issue_row(101, "2")])
    fixture["responses"].append(httpx.Response(200, json=complete))
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={key: value for key, value in fixture["body"].items() if key != "source_revision"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["issues"]["data"]["total"] == 2
    assert preview.json()["series"]["data"]["issue_count"] == 1
    assert not list(fixture["root"].iterdir())
    latest = {**complete, "count": 3} if invalid_after_preview else complete
    fixture["responses"].append(httpx.Response(200, json=latest))
    added = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert added.status_code == (409 if invalid_after_preview else 201), added.text
    assert [call.url.path for call in fixture["calls"]] == [
        "/api/series/8/",
        "/api/series/8/issue_list/",
        "/api/series/8/",
        "/api/series/8/issue_list/",
    ]
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Issue)) == (
            0 if invalid_after_preview else 2
        )
    if invalid_after_preview:
        assert not fixture["events"] and not list(fixture["root"].iterdir())
    else:
        assert added.json()["issue_count"] == 2
        assert len(fixture["events"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": 0},
        {"source_revision": 99},
    ],
)
async def test_stale_selection_rejects_before_provider_calls(
    authenticated_client, source_add_setup, changes
):
    fixture = source_add_setup
    response = await authenticated_client.post(
        "/api/v1/series", json={**fixture["body"], **changes}, headers=csrf(authenticated_client)
    )
    assert response.status_code == 409
    assert not fixture["calls"] and not list(fixture["root"].iterdir())


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Injected"},
        {"comicvine_id": 999},
        {"external_id": "../8"},
        {"source_revision": True},
        {"issues": []},
        {"source": "locg"},
    ],
)
async def test_rejects_client_metadata_or_ambiguous_identity(
    authenticated_client, source_add_setup, changes
):
    fixture = source_add_setup
    response = await authenticated_client.post(
        "/api/v1/series", json={**fixture["body"], **changes}, headers=csrf(authenticated_client)
    )
    assert response.status_code == 422
    assert not fixture["calls"]


async def test_failed_catalog_does_not_create_partial_library(
    authenticated_client, source_add_setup, sec_db
):
    fixture = source_add_setup
    fixture["responses"].append(httpx.Response(404))
    response = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert response.status_code == 409
    assert not fixture["events"] and not list(fixture["root"].iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


async def test_add_requires_auth_and_csrf(
    authenticated_client, unauthenticated_client, source_add_setup
):
    fixture = source_add_setup
    assert (
        await unauthenticated_client.post("/api/v1/series", json=fixture["body"])
    ).status_code in {401, 403}
    assert (
        await authenticated_client.post("/api/v1/series", json=fixture["body"])
    ).status_code == 403
    assert not fixture["calls"]
