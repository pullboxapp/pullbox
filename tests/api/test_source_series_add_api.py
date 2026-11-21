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


async def test_untagged_review_add_uses_real_catalog_then_explicit_issue_and_rollback(
    authenticated_client, source_add_setup, sec_db, tmp_path, monkeypatch
):
    import zipfile
    from unittest.mock import AsyncMock

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus
    from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
    from tests.integration.test_metron_folder_import import _service
    from tests.ui.test_import_library_search import discovery
    from tests.ui.test_import_review_file_routes import token

    fixture = source_add_setup
    comic = tmp_path / "incoming" / "Unsorted" / "unidentified.cbz"
    comic.parent.mkdir(parents=True)
    with zipfile.ZipFile(comic, "w") as archive:
        archive.writestr("001.jpg", b"synthetic page one")
        archive.writestr("002.jpg", b"synthetic page two")
    original = comic.read_bytes()
    service = _service()
    async with sec_db() as session:
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_type="filesystem",
                source_path=str(tmp_path / "incoming"),
                target_library_root_id=fixture["body"]["library_root_id"],
            ),
        )
        await service.start_scan(session, job.id)
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.import_job_id == job.id)
        )
        job_id, parent_id, file_id = job.id, file.import_series_id, file.id
    monkeypatch.setattr(
        "pullbox.ui.series_routes.search_snapshot",
        AsyncMock(return_value=discovery(identifier="8")),
    )
    result = await authenticated_client.get(
        f"/import/{job_id}/series/{parent_id}/review-library-search?q=Fixture"
    )
    assert result.status_code == 200
    assert "Preview &amp; add" in result.text
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={key: value for key, value in fixture["body"].items() if key != "source_revision"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["issues"]["data"]["total"] == 3
    added = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert added.status_code == 201, added.text
    series_id = added.json()["id"]
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
        issue_id = await session.scalar(
            select(Issue.id).where(
                Issue.series_id == series_id, func.lower(Issue.issue_number_text) == "13a"
            )
        )
    url = f"/import/{job_id}/series/{parent_id}/review-library"
    choice = await authenticated_client.get(f"{url}?library_series_id={series_id}")
    assert choice.status_code == 200
    selected = await authenticated_client.post(
        url,
        data={"token": token(choice.text), "library_series_id": series_id},
        headers=csrf(authenticated_client),
    )
    assert selected.status_code == 200
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    choice = await authenticated_client.get(url + "?q=13a")
    selected = await authenticated_client.post(
        url,
        data={"token": token(choice.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert selected.status_code == 200, selected.text
    async with sec_db() as session:
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[parent_id]))
        await service.run_import(session, job_id)
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.IMPORTED, file.error_message
        assert await service.rollback_import(session, job_id)
        assert await session.get(Series, series_id) is not None
    assert comic.read_bytes() == original


async def test_add_from_metron_import_rechecks_same_job_and_preserves_series_on_rollback(
    authenticated_client, source_add_setup, sec_db, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
    from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
    from tests.integration.test_metron_folder_import import _archive, _service

    fixture = source_add_setup
    source = tmp_path / "incoming"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic, series_id="8", issue_id="101", number="13b")
    service = _service()
    async with sec_db() as session:
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_type="filesystem",
                source_path=str(source),
                target_library_root_id=fixture["body"]["library_root_id"],
            ),
        )
        await service.start_scan(session, job.id)
        file = (await session.scalars(select(ImportedFile))).one()
        job_id, group_id, file_id = job.id, file.import_series_id, file.id
        assert file.status == ImportedFileStatus.NO_MATCH
        await session.commit()
    assert not fixture["calls"]
    context = await authenticated_client.get(f"/import/{job_id}/series/{group_id}/metron-preview")
    assert context.status_code == 200
    payload = context.json()
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={key: payload[key] for key in ("source", "external_id", "library_root_id")},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200
    added = await authenticated_client.post(
        "/api/v1/series", json=fixture["body"], headers=csrf(authenticated_client)
    )
    assert added.status_code == 201, added.text
    series_id = added.json()["id"]
    trigger = Mock()
    monkeypatch.setattr("pullbox.tasks.import_task.trigger_import_series_rematch", trigger)
    recheck = await authenticated_client.post(
        f"/import/{job_id}/series/{group_id}/metron-recheck", headers=csrf(authenticated_client)
    )
    assert recheck.status_code == 202, recheck.text
    trigger.assert_called_once_with(job_id, group_id)
    async with sec_db() as session:
        await service.rematch_imported_series_files(session, job_id, group_id)
        file = await session.get(ImportedFile, file_id)
        group = await session.get(ImportedSeries, group_id)
        assert file.status == ImportedFileStatus.MATCHED and not file.include_in_import
        assert group.series_id == series_id
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[group_id]))
        await service.run_import(session, job_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.IMPORTED
        assert await service.rollback_import(session, job_id)
        assert await session.get(Series, series_id) is not None
        assert comic.read_bytes() == original


@pytest.mark.parametrize("invalid_after_preview", [False, True])
@pytest.mark.parametrize("summary_count", [1, 1000001, 1000000000])
async def test_stale_summary_preview_and_final_add_revalidate_the_catalog(
    authenticated_client, source_add_setup, sec_db, invalid_after_preview, summary_count
):
    fixture = source_add_setup
    fixture["profile"]["issue_count"] = summary_count
    complete = envelope([issue_row(100, "1"), issue_row(101, "2")])
    fixture["responses"].append(httpx.Response(200, json=complete))
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={key: value for key, value in fixture["body"].items() if key != "source_revision"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["issues"]["data"]["total"] == 2
    assert preview.json()["series"]["data"]["issue_count"] == summary_count
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
