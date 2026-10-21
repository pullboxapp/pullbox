"""Explicit series propagation preserves canonical records and comic pages."""

from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.models import Issue, LibraryFile, Series
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType
from tests.api.test_issue_archive_metadata import sec_db as _sec_db
from tests.api.test_metadata_sources_api import csrf
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared

pytest_plugins = ["conftest_security"]
sec_db = _sec_db


@pytest.fixture
def queue(sec_db, monkeypatch):
    import pullbox.api.v1.series_metadata_links as api

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    return manager


async def seed_pair(factory, path, staged, plan):
    path.write_bytes(staged.path.read_bytes())
    async with factory.begin() as session:
        file = await session.get(LibraryFile, plan.target.binding.library_file_id)
        file.file_size = path.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        series = await session.get(Series, plan.target.binding.metadata.series.local_id)
        series.title = "Corrected series"
        issue = await session.get(Issue, file.issue_id)
        issue.description = "New library summary must not propagate"


async def test_preview_then_selected_write_preserves_canonical_and_pages(
    authenticated_client, sec_db, tmp_path, queue
):
    async with prepared(sec_db, tmp_path) as (path, staged, plan):
        await seed_pair(sec_db, path, staged, plan)
        series_id = plan.target.binding.metadata.series.local_id
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/series/{series_id}/metadata-propagation"
        before = path.read_bytes()
        response = await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        assert response.status_code == 202, response.text
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert report["counts"]["ready"] == 1, report
        assert report["targets"][0]["changes"]
        assert path.read_bytes() == before
        async with sec_db() as session:
            series_before = (await session.get(Series, series_id)).updated_at
            issue_before = (await session.get(Issue, issue_id)).updated_at
        response = await authenticated_client.post(
            endpoint + "/write",
            json={"job_id": report["job"]["id"], "issue_ids": [issue_id]},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 202, response.text
        await queue.dispatch_next()
        result = (await authenticated_client.get(endpoint + "/job")).json()
        assert result["counts"]["written"] == 1, result
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            ci = ET.fromstring(archive.read("ComicInfo.xml"))
            mi = ET.fromstring(archive.read("MetronInfo.xml"))
            assert ci.findtext("Series") == mi.findtext("Series/Name") == "Corrected series"
            assert not ci.findtext("Summary") and not mi.findtext("Summary")
        async with sec_db() as session:
            assert (await session.get(Series, series_id)).updated_at == series_before
            issue = await session.get(Issue, issue_id)
            assert issue.updated_at == issue_before
            assert issue.description == "New library summary must not propagate"
        again = await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        assert again.status_code == 202
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert report["counts"]["current"] == 1, report


@pytest.mark.parametrize("change", ["series", "file"])
async def test_changed_approval_is_actionable_without_rewriting(
    authenticated_client, sec_db, tmp_path, queue, change
):
    async with prepared(sec_db, tmp_path) as (path, staged, plan):
        await seed_pair(sec_db, path, staged, plan)
        endpoint = (
            f"/api/v1/series/{plan.target.binding.metadata.series.local_id}/metadata-propagation"
        )
        await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        if change == "series":
            async with sec_db.begin() as session:
                series = await session.get(Series, plan.target.binding.metadata.series.local_id)
                series.title = "Changed after preview"
        else:
            with ZipFile(path, "a") as archive:
                archive.writestr("new-page.jpg", b"new content")
        original = path.read_bytes()
        response = await authenticated_client.post(
            endpoint + "/write",
            json={"job_id": report["job"]["id"]},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 202, response.text
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert report["counts"]["attention"] == 1, report
        assert "Preview again" in report["targets"][0]["reason"]
        assert path.read_bytes() == original


async def test_missing_pair_is_reviewable_not_silently_filled_or_written(
    authenticated_client, sec_db, tmp_path, queue
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        endpoint = (
            f"/api/v1/series/{plan.target.binding.metadata.series.local_id}/metadata-propagation"
        )
        original = path.read_bytes()
        response = await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        assert response.status_code == 202
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert report["counts"]["attention"] == 1, report
        assert "Write file metadata" in report["targets"][0]["reason"]
        assert path.read_bytes() == original


@pytest.mark.parametrize("fields", [[], ["description"], ["issue_count"], ["issue_number_text"]])
async def test_only_explicit_shared_fields_are_accepted(authenticated_client, fields):
    response = await authenticated_client.post(
        "/api/v1/series/1/metadata-propagation/preview",
        json={"fields": fields},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 422


async def test_cross_series_approval_and_unknown_selection_are_rejected(
    authenticated_client, sec_db, tmp_path, queue
):
    async with prepared(sec_db, tmp_path) as (path, staged, plan):
        await seed_pair(sec_db, path, staged, plan)
        endpoint = (
            f"/api/v1/series/{plan.target.binding.metadata.series.local_id}/metadata-propagation"
        )
        no_csrf = await authenticated_client.post(endpoint + "/preview", json={"fields": ["title"]})
        assert no_csrf.status_code == 403
        await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        for url, selected in [
            (endpoint, [99999]),
            ("/api/v1/series/99999/metadata-propagation", None),
        ]:
            response = await authenticated_client.post(
                url + "/write",
                json={"job_id": report["job"]["id"], "issue_ids": selected},
                headers=csrf(authenticated_client),
            )
            assert response.status_code == 409


async def test_retry_recovers_published_receipt_without_rewriting(
    authenticated_client, sec_db, tmp_path, queue, monkeypatch
):
    import pullbox.utilities.executors.series_metadata_propagation as executor

    writer = executor.write_file_metadata

    async def lost_response(*args, **kwargs):
        await writer(*args, **kwargs)
        raise OSError("Simulated lost job completion after durable publication")

    async with prepared(sec_db, tmp_path) as (path, staged, plan):
        await seed_pair(sec_db, path, staged, plan)
        series_id = plan.target.binding.metadata.series.local_id
        endpoint = f"/api/v1/series/{series_id}/metadata-propagation"
        await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        await authenticated_client.post(
            endpoint + "/write",
            json={"job_id": report["job"]["id"]},
            headers=csrf(authenticated_client),
        )
        monkeypatch.setattr(executor, "write_file_metadata", lost_response)
        await queue.dispatch_next()
        failed = (await authenticated_client.get(endpoint + "/job")).json()
        assert failed["retryable"] and failed["counts"]["attention"] == 1
        before = path.read_bytes(), path.stat()
        monkeypatch.setattr(executor, "write_file_metadata", writer)
        retry = await authenticated_client.post(
            endpoint + "/retry",
            json={"job_id": failed["job"]["id"]},
            headers=csrf(authenticated_client),
        )
        assert retry.status_code == 202, retry.text
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert report["counts"]["written"] == 1, report
        assert (path.read_bytes(), path.stat()) == before


async def test_referenced_file_and_import_protection_remain_fail_closed(
    authenticated_client, sec_db, tmp_path, queue, monkeypatch
):
    import pullbox.services.issue_file_metadata as service
    from pullbox.models.library import LibraryFileStorageMode
    from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError

    async with prepared(sec_db, tmp_path) as (path, staged, plan):
        await seed_pair(sec_db, path, staged, plan)
        endpoint = (
            f"/api/v1/series/{plan.target.binding.metadata.series.local_id}/metadata-propagation"
        )
        original = path.read_bytes(), path.stat()
        async with sec_db.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.storage_mode = LibraryFileStorageMode.REFERENCED
        await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert "kept in place" in report["targets"][0]["reason"]
        async with sec_db.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.storage_mode = LibraryFileStorageMode.MANAGED

        async def protected(*args, **kwargs):
            raise ArchiveMetadataBindingError("import_operation_active")

        monkeypatch.setattr(service, "_require_no_import_owner", protected)
        await authenticated_client.post(
            endpoint + "/preview", json={"fields": ["title"]}, headers=csrf(authenticated_client)
        )
        await queue.dispatch_next()
        report = (await authenticated_client.get(endpoint + "/job")).json()
        assert "unfinished import" in report["targets"][0]["reason"]
        assert (path.read_bytes(), path.stat()) == original
