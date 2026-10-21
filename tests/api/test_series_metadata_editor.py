"""Series corrections save canonical metadata without propagating into comics."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.library import FileFormat
from pullbox.models.metadata_baseline import SeriesMetadataBaseline
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType, UtilityJob, UtilityJobItem
from tests.api.test_issue_archive_metadata import sec_db as _sec_db
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_series_sidecar import sidecar_target as _sidecar_target

pytest_plugins = ["conftest_security"]
sec_db = _sec_db
sidecar_target = _sidecar_target


@pytest.fixture
def metadata_queue(sec_db, monkeypatch):
    import pullbox.api.v1.series_metadata_links as api

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None, raising=False)
    return manager


def endpoint(series_id):
    return f"/api/v1/series/{series_id}/metadata-editor"


async def approve(client, series_id, body):
    preview = await client.post(endpoint(series_id) + "/preview", json=body, headers=csrf(client))
    assert preview.status_code == 200, preview.text
    assert preview.json()["ready"]
    response = await client.post(
        endpoint(series_id) + "/write",
        json={**body, "review_key": preview.json()["review_key"]},
        headers=csrf(client),
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_default_save_commits_canonical_and_durable_sidecar_job(
    authenticated_client, sidecar_target, sec_db, metadata_queue, monkeypatch
):
    from pullbox.services.metadata_discovery import MetadataSourceRegistry

    async def forbidden(*args, **kwargs):
        raise AssertionError("Series editing must never fetch providers")

    monkeypatch.setattr(MetadataSourceRegistry, "series", forbidden)
    series_id, folder, _ = sidecar_target
    body = {"edits": {"title": {"action": "set", "value": "Corrected display title"}}}
    receipt = await approve(authenticated_client, series_id, body)
    assert receipt["library_saved"] is True
    assert not (folder / "series.json").exists()
    async with sec_db() as session:
        series = await session.get(Series, series_id)
        assert series.title == "Corrected display title"
        assert series.sort_title == "test series"
        assert series.issue_count == 3
        assert series.comicvine_id == 42
        assert series.path == str(folder)
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        snapshot = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
        assert next(o for o in snapshot.origins if o.field == "title").user_override
    await metadata_queue.dispatch_next()
    saved = json.loads((folder / "series.json").read_bytes())
    assert saved["name"] == "Corrected display title"
    assert saved["pullbox"]["snapshot"] == snapshot.model_dump(mode="json")
    report = await authenticated_client.get(endpoint(series_id) + "/job")
    assert report.json()["job"]["state"] == "COMPLETED", report.text
    assert report.json()["targets"][0]["state"] == "COMPLETED"
    assert not report.json()["retryable"]


async def test_library_only_never_inspects_invalid_sidecars(
    authenticated_client, sidecar_target, metadata_queue, monkeypatch
):
    from pullbox.services import series_sidecar

    series_id, folder, _ = sidecar_target
    path = folder / "series.json"
    path.write_bytes(b"not json, deliberately unreadable")

    def forbidden(*args, **kwargs):
        raise AssertionError("Library-only editing must not inspect sidecars")

    monkeypatch.setattr(series_sidecar, "_inspect_all", forbidden)
    await approve(
        authenticated_client,
        series_id,
        {"mode": "library_only", "edits": {"description": {"action": "clear"}}},
    )
    await metadata_queue.dispatch_next()
    assert path.read_bytes() == b"not json, deliberately unreadable"


async def test_editor_stale_approval_and_csrf_fail_without_saving(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    body = {"edits": {"title": {"action": "set", "value": "Reviewed"}}}
    assert (
        await authenticated_client.post(endpoint(series_id) + "/preview", json=body)
    ).status_code == 403
    preview = await authenticated_client.post(
        endpoint(series_id) + "/preview", json=body, headers=csrf(authenticated_client)
    )
    assert preview.status_code == 200, preview.text
    (folder / "series.json").write_text('{"custom":"New note"}')
    response = await authenticated_client.post(
        endpoint(series_id) + "/write",
        json={**body, "review_key": preview.json()["review_key"]},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 409, response.text
    async with sec_db() as session:
        assert (await session.get(Series, series_id)).title == "Test series"


@pytest.mark.parametrize(
    "edits",
    [
        {"issue_count": {"action": "set", "value": 7}},
        {"comicvine_id": {"action": "set", "value": 99}},
        {"title": {"action": "clear"}},
        {"title": {"action": "set", "value": "   "}},
        {"year_start": {"action": "set", "value": True}},
        {"status": {"action": "set", "value": "bogus"}},
        {"series_type": {"action": "set", "value": "bogus"}},
        {"volume": {"action": "reset", "value": "injected"}},
    ],
)
async def test_protected_or_invalid_fields_are_rejected(
    authenticated_client, sidecar_target, edits
):
    series_id, _, _ = sidecar_target
    response = await authenticated_client.post(
        endpoint(series_id) + "/preview", json={"edits": edits}, headers=csrf(authenticated_client)
    )
    assert response.status_code in {409, 422}, response.text


async def test_select_all_library_keeps_empty_optional_fields(
    authenticated_client, sidecar_target, metadata_queue
):
    series_id, _, _ = sidecar_target
    preview = await authenticated_client.post(
        endpoint(series_id) + "/preview", json={}, headers=csrf(authenticated_client)
    )
    assert preview.status_code == 200, preview.text
    editable = [field for field in preview.json()["editor_fields"] if field["editable"]]
    assert any(field["current"] == "" for field in editable)
    body = {
        "edits": {field["key"].removeprefix("series."): {"action": "library"} for field in editable}
    }
    await approve(authenticated_client, series_id, body)


async def test_partial_retry_retains_only_failed_destinations_without_resaving_library(
    authenticated_client, sidecar_target, sec_db, metadata_queue, monkeypatch
):
    from pullbox.services import series_sidecar
    from pullbox.services.series_metadata_editor import read_editor_plan

    series_id, first, root_path = sidecar_target
    second = root_path / "Second"
    second.mkdir()
    comic = second / "issue.cbz"
    comic.write_bytes(b"Untouched comic bytes")
    async with sec_db.begin() as session:
        series = await session.get(Series, series_id)
        issue = Issue(series_id=series_id, issue_number=1)
        session.add(issue)
        await session.flush()
        session.add(
            LibraryFile(
                file_path=str(comic),
                file_name=comic.name,
                file_format=FileFormat.CBZ,
                file_size=comic.stat().st_size,
                file_modified_at=datetime.now(UTC),
                issue_id=issue.id,
                library_root_id=series.library_root_id,
            )
        )
    original = series_sidecar._stage

    def fail_second(target):
        if target.location.directory == str(second):
            raise OSError("Test full disk")
        return original(target)

    monkeypatch.setattr(series_sidecar, "_stage", fail_second)
    receipt = await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"title": {"action": "set", "value": "Saved despite partial sidecar failure"}},
        },
    )
    await metadata_queue.dispatch_next()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["partial"] and report["retryable"]
    assert {target["state"] for target in report["targets"]} == {"COMPLETED", "FAILED"}
    assert "attention" in report["job"]["message"]
    first_bytes, first_stat = (first / "series.json").read_bytes(), (first / "series.json").stat()
    async with sec_db() as session:
        items = list(
            await session.scalars(
                select(UtilityJobItem).where(UtilityJobItem.job_id == receipt["job_id"])
            )
        )
        assert {json.loads(item.after_state)["outcome"] for item in items} == {"saved", "failed"}
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        revision = baseline.revision
    monkeypatch.setattr(series_sidecar, "_stage", original)
    response = await authenticated_client.post(
        endpoint(series_id) + "/retry",
        json={"job_id": receipt["job_id"]},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 202, response.text
    async with sec_db() as session:
        job = await session.get(UtilityJob, response.json()["job_id"])
        plan = read_editor_plan(json.loads(job.config))
        assert [target.location.directory for target in plan.targets] == [str(second)]
    await metadata_queue.dispatch_next()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert not report["partial"] and not report["retryable"]
    assert (second / "series.json").exists()
    assert ((first / "series.json").read_bytes(), (first / "series.json").stat()) == (
        first_bytes,
        first_stat,
    )
    assert comic.read_bytes() == b"Untouched comic bytes"
    async with sec_db() as session:
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        assert baseline.revision == revision


async def test_cancel_before_dispatch_keeps_saved_library_and_can_retry_original_target(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    receipt = await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"volume": {"action": "set", "value": "2"}},
        },
    )
    async with sec_db.begin() as session:
        await metadata_queue.cancel_job(session, receipt["job_id"])
    await metadata_queue.dispatch_next()
    assert not (folder / "series.json").exists()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["job"]["state"] == "CANCELLED"
    assert report["library_saved"] and report["retryable"]
    async with sec_db() as session:
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        assert MetadataSnapshot.model_validate_json(baseline.snapshot_json).values.volume == "2"
    response = await authenticated_client.post(
        endpoint(series_id) + "/retry",
        json={"job_id": receipt["job_id"]},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 202, response.text
    await metadata_queue.dispatch_next()
    assert (folder / "series.json").exists()


async def test_blocked_sidecar_is_visible_but_never_automatically_retryable(
    authenticated_client, sidecar_target, metadata_queue
):
    series_id, folder, _ = sidecar_target
    path = folder / "series.json"
    path.write_bytes(b"Invalid sidecar, preserve exactly")
    await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"language": {"action": "set", "value": "en"}},
        },
    )
    await metadata_queue.dispatch_next()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["partial"] and not report["retryable"]
    assert report["warnings"] and report["targets"][0]["reason"]
    assert report["targets"][0]["state"] == "SKIPPED"
    assert path.read_bytes() == b"Invalid sidecar, preserve exactly"


async def test_saved_sidecar_source_is_server_bound_and_never_provider_attributed(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    (folder / "series.json").write_text('{"ComicID":42,"description":"Saved description"}')
    preview = await authenticated_client.post(
        endpoint(series_id) + "/preview", json={}, headers=csrf(authenticated_client)
    )
    assert preview.status_code == 200, preview.text
    field = next(
        field for field in preview.json()["editor_fields"] if field["key"] == "series.description"
    )
    source = next(source for source in field["sources"] if source["token"] != "library")
    assert source["value"] == "Saved description"
    assert field["label"] == "Description"
    rejected = await authenticated_client.post(
        endpoint(series_id) + "/preview",
        json={
            "edits": {
                "description": {"action": "sidecar", "source": source["token"], "value": "Injected"}
            }
        },
        headers=csrf(authenticated_client),
    )
    assert rejected.status_code == 422, rejected.text
    rejected = await authenticated_client.post(
        endpoint(series_id) + "/preview",
        json={"edits": {"description": {"action": "library"}}},
        headers=csrf(authenticated_client),
    )
    assert rejected.status_code == 409, rejected.text
    await approve(
        authenticated_client,
        series_id,
        {"edits": {"description": {"action": "sidecar", "source": source["token"]}}},
    )
    async with sec_db() as session:
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        snapshot = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
        assert snapshot.values.description == "Saved description"
        origin = next(origin for origin in snapshot.origins if origin.field == "description")
        assert origin.user_override and origin.source is None


async def test_job_creation_failure_rolls_back_canonical_save(
    authenticated_client, sidecar_target, sec_db, metadata_queue, monkeypatch
):
    series_id, _, _ = sidecar_target
    body = {
        "mode": "library_only",
        "edits": {"title": {"action": "set", "value": "Must roll back"}},
    }
    preview = await authenticated_client.post(
        endpoint(series_id) + "/preview", json=body, headers=csrf(authenticated_client)
    )
    assert preview.status_code == 200, preview.text

    async def fail(*args, **kwargs):
        raise ValueError("Test job admission failure")

    monkeypatch.setattr(metadata_queue, "create_job", fail)
    response = await authenticated_client.post(
        endpoint(series_id) + "/write",
        json={**body, "review_key": preview.json()["review_key"]},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 409, response.text
    async with sec_db() as session:
        assert (await session.get(Series, series_id)).title == "Test series"
        assert (
            await session.scalar(
                select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
            )
            is None
        )


async def test_unrelated_unsupported_artwork_does_not_block_canonical_correction(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    async with sec_db.begin() as session:
        (await session.get(Series, series_id)).cover_url = "data:image/png;base64,fixture"
    await approve(
        authenticated_client,
        series_id,
        {"mode": "library_only", "edits": {"title": {"action": "set", "value": "Display only"}}},
    )
    await metadata_queue.dispatch_next()
    async with sec_db() as session:
        assert (await session.get(Series, series_id)).cover_url == "data:image/png;base64,fixture"
    assert not (folder / "series.json").exists()


async def test_repeated_selected_value_keeps_sidecar_provenance_equal_to_saved_baseline(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    body = {"edits": {"title": {"action": "set", "value": "Correction"}}}
    for _ in range(2):
        await approve(authenticated_client, series_id, body)
        await metadata_queue.dispatch_next()
    async with sec_db() as session:
        baseline = await session.scalar(
            select(SeriesMetadataBaseline).where(SeriesMetadataBaseline.series_id == series_id)
        )
        snapshot = json.loads(baseline.snapshot_json)
    assert json.loads((folder / "series.json").read_bytes())["pullbox"]["snapshot"] == snapshot
