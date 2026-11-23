"""Single-issue editing reuses the guarded pair writer without touching siblings."""

from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models import Issue, LibraryFile
from pullbox.models.library import LibraryFileStorageMode
from pullbox.models.metadata_baseline import IssueMetadataBaseline
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType
from tests.api.test_issue_archive_metadata import sec_db as _sec_db
from tests.api.test_metadata_sources_api import csrf
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared

pytest_plugins = ["conftest_security"]
sec_db = _sec_db


async def test_editor_exposes_clean_empty_and_protected_fields_without_writing(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        before = path.read_bytes(), path.stat()
        response = await authenticated_client.post(
            f"/api/v1/issues/{issue_id}/metadata-editor/preview",
            json={},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 200, response.text
        fields = {item["key"]: item for item in response.json()["editor_fields"]}
        assert fields["issue.title"]["editable"]
        assert fields["issue.description"]["current"] == ""
        assert not fields["issue.issue_number_text"]["editable"]
        assert not fields["series.title"]["editable"]
        assert fields["issue.age_rating"]["editable"]
        assert not response.json()["editor_changed"]
        assert (path.read_bytes(), path.stat()) == before


async def test_preview_accepts_all_editable_fields_together(authenticated_client, sec_db, tmp_path):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/metadata-editor/preview"
        headers = csrf(authenticated_client)
        before = path.read_bytes()
        initial = await authenticated_client.post(endpoint, json={}, headers=headers)
        assert initial.status_code == 200
        keys = {
            f["key"].removeprefix("issue.")
            for f in initial.json()["editor_fields"]
            if f["editable"]
        }
        assert {"genres", "tags", "characters", "teams", "locations"} <= keys
        edits = {key: {"source": "library"} for key in keys}
        report = await authenticated_client.post(endpoint, json={"edits": edits}, headers=headers)
        assert report.status_code == 200, report.text
        assert {
            f["key"].removeprefix("issue.") for f in report.json()["editor_fields"] if f["selected"]
        } == keys
        assert path.read_bytes() == before


async def test_editor_custom_title_writes_both_xml_and_persists_override(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    import pullbox.api.v1.issue_metadata_links as api

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with sec_db.begin() as session:
            issue = await session.get(Issue, issue_id)
            issue.description = "Keep this unselected summary."
        endpoint = f"/api/v1/issues/{issue_id}/metadata-editor"
        body = {"edits": {"title": {"source": "custom", "value": "Planet Atmos correction"}}}
        before = path.read_bytes()
        preview = await authenticated_client.post(
            endpoint + "/preview", json=body, headers=csrf(authenticated_client)
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["editor_changed"]
        assert path.read_bytes() == before
        response = await authenticated_client.post(
            endpoint + "/write",
            json={**body, "review_key": preview.json()["review_key"]},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 202, response.text
        assert path.read_bytes() == before
        await manager.dispatch_next()
        report = await authenticated_client.get(f"/api/v1/issues/{issue_id}/file-metadata/job")
        assert report.json()["job"]["state"] == "COMPLETED", report.text
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert ET.fromstring(archive.read("ComicInfo.xml")).findtext("Title") == (
                "Planet Atmos correction"
            )
            assert ET.fromstring(archive.read("MetronInfo.xml")).findtext("Stories/Story") == (
                "Planet Atmos correction"
            )
        async with sec_db() as session:
            issue = await session.get(Issue, issue_id)
            assert issue.title == "Planet Atmos correction"
            assert issue.description == "Keep this unselected summary."
            assert issue.issue_number_text == "50-X"
            baseline = await session.scalar(
                select(IssueMetadataBaseline).where(IssueMetadataBaseline.issue_id == issue_id)
            )
            snapshot = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
            assert any(o.field == "title" and o.user_override for o in snapshot.origins)


async def test_editor_can_correct_agreeing_ratings_and_explicitly_clear_both(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    import pullbox.api.v1.issue_metadata_links as api

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/metadata-editor"

        async def preview(edits):
            response = await authenticated_client.post(
                endpoint + "/preview", json={"edits": edits}, headers=csrf(authenticated_client)
            )
            assert response.status_code == 200, response.text
            return response.json()

        for old, new in [
            (None, "Teen"),
            ("Teen", "Everyone"),
            ("Everyone", "Unknown"),
            ("Unknown", None),
        ]:
            before = path.read_bytes()
            edit = {"source": "custom", "value": new} if new else {"source": "clear"}
            edits = {"age_rating": edit}
            report = await preview(edits)
            assert report["ready"] and report["editor_changed"]
            assert {
                c["document"]
                for c in report["changes"]
                if "AgeRating" in c["field"] and c["before"] == old and c["after"] == new
            } == {"ComicInfo.xml", "MetronInfo.xml"}
            assert path.read_bytes() == before
            response = await authenticated_client.post(
                endpoint + "/write",
                json={"edits": edits, "review_key": report["review_key"]},
                headers=csrf(authenticated_client),
            )
            assert response.status_code == 202, response.text
            await manager.dispatch_next()
            job = await authenticated_client.get(f"/api/v1/issues/{issue_id}/file-metadata/job")
            assert job.json()["job"]["state"] == "COMPLETED", job.text
            with ZipFile(path) as archive:
                for document in ("ComicInfo.xml", "MetronInfo.xml"):
                    assert ET.fromstring(archive.read(document)).findtext("AgeRating") == new
                assert archive.read("page.jpg") == b"page bytes"
            fields = {f["key"]: f for f in (await preview({}))["editor_fields"]}
            assert fields["issue.age_rating"]["current"] == (new or "")
            assert fields["issue.age_rating"]["overridden"]
            assert not fields["issue.age_rating"]["differs"]


@pytest.mark.parametrize("source,value", [("ComicInfo.xml", "PG"), ("MetronInfo.xml", "Explicit")])
async def test_editor_opens_when_embedded_rating_has_no_exact_equivalent(
    authenticated_client, sec_db, tmp_path, source, value
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            root = source.split(".")[0]
            parent = "<Series><Name>Example</Name></Series>" if root == "MetronInfo" else ""
            archive.writestr(
                source,
                f"<{root}>{parent}<Number>50-X</Number><AgeRating>{value}</AgeRating></{root}>",
            )
        async with sec_db.begin() as session:
            row = await session.get(LibraryFile, plan.target.binding.library_file_id)
            row.file_size = path.stat().st_size
            row.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        before = path.read_bytes()
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/metadata-editor/preview"

        async def preview(edits):
            return await authenticated_client.post(
                endpoint, json={"edits": edits}, headers=csrf(authenticated_client)
            )

        result = await preview({})
        assert result.status_code == 200, result.text
        assert not result.json()["ready"]
        rating = next(f for f in result.json()["editor_fields"] if f["key"] == "issue.age_rating")
        assert {"value": value, "source": source, "selectable": False} in rating["options"]
        assert (await preview({"age_rating": {"source": source}})).status_code == 409
        corrected = await preview({"age_rating": {"source": "custom", "value": "Teen"}})
        assert corrected.status_code == 200 and corrected.json()["ready"], corrected.text
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "edits",
    [
        {"issue_number_text": {"source": "custom", "value": "7"}},
        {"series.title": {"source": "custom", "value": "Other series"}},
        {"page_count": {"source": "custom", "value": "-1"}},
        {"cover_date": {"source": "custom", "value": "2026-02-30"}},
        {"title": {"source": "library", "value": "Client-injected value"}},
        {"age_rating": {"source": "custom", "value": "PG-13"}},
        {"tags": {"source": "custom", "value": "City\ncity"}},
        {"tags": {"source": "custom", "value": "City,Night"}},
        {"tags": {"source": "custom", "value": "x" * 256}},
        {"tags": {"source": "custom", "value": "\n".join(map(str, range(129)))}},
    ],
)
async def test_editor_rejects_invalid_or_protected_edits(
    authenticated_client, sec_db, tmp_path, edits
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        before = path.read_bytes()
        response = await authenticated_client.post(
            f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/metadata-editor/preview",
            json={"edits": edits},
            headers=csrf(authenticated_client),
        )
        assert response.status_code in {409, 422}, response.text
        assert path.read_bytes() == before


async def test_editor_revalidates_approval_and_requires_csrf(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/metadata-editor"
        body = {"edits": {"title": {"source": "custom", "value": "Reviewed"}}}
        assert (
            await authenticated_client.post(endpoint + "/preview", json=body)
        ).status_code == 403
        preview = await authenticated_client.post(
            endpoint + "/preview", json=body, headers=csrf(authenticated_client)
        )
        assert preview.status_code == 200, preview.text
        before = path.read_bytes()
        body["edits"]["title"]["value"] = "Not reviewed"
        response = await authenticated_client.post(
            endpoint + "/write",
            json={**body, "review_key": preview.json()["review_key"]},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409
        assert path.read_bytes() == before


async def test_editor_does_not_convert_or_mutate_referenced_files(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with sec_db.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.storage_mode = LibraryFileStorageMode.REFERENCED
        before = path.read_bytes()
        response = await authenticated_client.post(
            f"/api/v1/issues/{issue_id}/metadata-editor/preview",
            json={},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409
        assert "kept in place" in response.text
        assert path.read_bytes() == before
        renamed = path.with_suffix(".cbr")
        path.rename(renamed)
        async with sec_db.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.storage_mode = LibraryFileStorageMode.MANAGED
            file.file_path = str(renamed)
            file.file_name = renamed.name
            file.file_modified_at = datetime.fromtimestamp(renamed.stat().st_mtime, UTC)
        response = await authenticated_client.post(
            f"/api/v1/issues/{issue_id}/metadata-editor/preview",
            json={},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409
        assert "Convert" in response.text
        assert renamed.read_bytes() == before
