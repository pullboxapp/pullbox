"""GCD wire records reach the real preview/Add workflow without live network I/O."""

import os
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.models import Issue, Series
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from tests.api.test_metadata_sources_api import csrf, policy
from tests.unit.test_gcd_api_v2 import TOKEN, issue_row, series_row, source

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
async def gcd_add_setup(authenticated_client, sec_app, sec_db, monkeypatch, tmp_path):
    from pullbox.api.deps import get_settings_dep
    from pullbox.providers.metadata import sources

    settings = get_settings().model_copy(update={"metadata_gcd_api_v2_enabled": True})
    sec_app.dependency_overrides[get_settings_dep] = lambda: settings
    monkeypatch.setattr("pullbox.api.v1.series.get_settings", lambda: settings)
    saved = await authenticated_client.put(
        "/api/v1/metadata/sources/gcd_api_v2",
        json=policy(credential=TOKEN),
        headers=csrf(authenticated_client),
    )
    assert saved.status_code == 200, saved.text
    root_path = tmp_path / "managed"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="GCD test", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        session.add(SystemConfig(key="search_on_add_default", value="true"))
        await session.flush()
        root_id = root.id
    calls, records = [], [issue_row(), issue_row(765610, "2")]

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/v2/series/50494/":
            return httpx.Response(200, json=series_row())
        assert request.url.path == "/api/v2/issues/"
        assert request.url.params["variant_of"] == "false"
        return httpx.Response(200, json={"count": len(records), "next": None, "results": records})

    monkeypatch.setattr(sources, "GcdApiV2Source", lambda _: source(handle))

    async def no_legacy(_session):
        pytest.fail("GCD Add must not construct a ComicVine service")

    monkeypatch.setattr("pullbox.api.v1.series._build_series_service", no_legacy)
    return {
        "body": {
            "source": "gcd_api_v2",
            "external_id": "50494",
            "source_revision": saved.json()["revision"],
            "library_root_id": root_id,
        },
        "calls": calls,
        "records": records,
        "root": root_path,
    }


async def test_real_gcd_shape_previews_and_adds_two_canonical_issues(
    authenticated_client,
    sec_db,
    gcd_add_setup,
):
    fixture = gcd_add_setup
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={key: value for key, value in fixture["body"].items() if key != "source_revision"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["series"]["data"]["title"] == "Badrock"
    assert preview.json()["issues"]["data"]["total"] == 2
    result = await authenticated_client.post(
        "/api/v1/series",
        json=fixture["body"],
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["comicvine_id"] is None
    assert data["issue_count"] == data["wanted_count"] == 2
    assert data["issue_catalog_state"] == "complete"
    assert Path(data["path"]).is_dir()
    repeated = await authenticated_client.post(
        "/api/v1/series",
        json=fixture["body"],
        headers=csrf(authenticated_client),
    )
    assert repeated.status_code == 201 and repeated.json()["id"] == data["id"]
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 2
        series_ids = list((await session.scalars(select(SeriesExternalIdentity))).all())
        issue_ids = list((await session.scalars(select(IssueExternalIdentity))).all())
        assert [(row.identity_namespace, row.external_id) for row in series_ids] == [
            ("gcd", "50494")
        ]
        assert {row.external_id for row in issue_ids} == {"765609", "765610"}
        assert all(row.identity_namespace == "gcd" for row in issue_ids)
    assert all(request.headers["authorization"] == f"Token {TOKEN}" for request in fixture["calls"])


async def test_wrong_gcd_parent_leaves_database_and_library_untouched(
    authenticated_client,
    sec_db,
    gcd_add_setup,
):
    fixture = gcd_add_setup
    fixture["records"][1]["series"] = {"id": 239118, "name": "Badrock"}
    result = await authenticated_client.post(
        "/api/v1/series",
        json=fixture["body"],
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409
    assert any(request.url.path == "/api/v2/issues/" for request in fixture["calls"])
    assert not list(fixture["root"].iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
        assert await session.scalar(select(func.count()).select_from(Issue)) == 0
