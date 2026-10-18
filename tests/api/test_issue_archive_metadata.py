"""Existing-file preview is read-only and approval queues only managed CBZs."""

import os
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Issue, LibraryFile
from pullbox.models.library import LibraryFileStorageMode
from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.issue_file_metadata import file_metadata_error
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType
from tests.api.test_metadata_sources_api import csrf
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
async def sec_db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    # Background progress and publication use independent sessions, not one StaticPool connection.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive-metadata.db'}")
    async with engine.begin() as connection:
        await create_test_schema(connection)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_background_sessions_have_independent_transactions(sec_db):
    async with sec_db() as writer, sec_db() as observer, writer.begin_nested():
        assert await writer.scalar(text("SELECT 1")) == 1
        assert await observer.scalar(text("SELECT 1")) == 1
        await observer.commit()


@pytest.mark.parametrize("field", ["title", "issue_count", "credits"])
def test_metadata_disagreement_names_the_field_without_exposing_values(field):
    message = file_metadata_error(ArchiveMetadataRenderError("unreconciled_field", field))
    assert f"disagree about {field.replace('_', ' ')}" in message
    assert "left unchanged" in message


async def test_managed_issue_can_preview_pair_without_writing(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        before = path.read_bytes(), path.stat()
        response = await authenticated_client.post(
            f"/api/v1/issues/{issue_id}/file-metadata/preview",
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["file_name"] == "example.cbz"
        assert data["documents"] == ["ComicInfo.xml", "MetronInfo.xml"]
        assert data["changes"] and len(data["review_key"]) == 64
        assert (path.read_bytes(), path.stat()) == before


async def test_story_extract_preview_reports_replacement_without_writing(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"story pages")
            archive.writestr(
                "ComicInfo.xml", "<ComicInfo><Number>158 (B Story)</Number></ComicInfo>"
            )
        async with sec_db.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.file_size = path.stat().st_size
            file.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        before = path.read_bytes()
        response = await authenticated_client.post(
            f"/api/v1/issues/{issue_id}/file-metadata/preview",
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409
        assert "story-only extract" in response.json()["detail"]
        assert "Import File" in response.json()["detail"]
        assert "Replace File" in response.json()["detail"]
        assert path.read_bytes() == before


async def test_referenced_issue_cannot_preview_a_write(authenticated_client, sec_db, tmp_path):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        async with sec_db.begin() as session:
            await session.execute(
                update(LibraryFile).values(storage_mode=LibraryFileStorageMode.REFERENCED)
            )
        before = path.read_bytes()
        response = await authenticated_client.post(
            f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/file-metadata/preview",
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409, response.text
        assert "kept in place" in response.text
        assert path.read_bytes() == before
        async with sec_db() as session:
            assert (
                await session.scalar(select(LibraryFile))
            ).storage_mode is LibraryFileStorageMode.REFERENCED


async def test_approval_queues_background_work_and_exposes_result(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    import pullbox.api.v1.issue_metadata_links as api

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/file-metadata"
        preview = await authenticated_client.post(
            endpoint + "/preview", headers=csrf(authenticated_client)
        )
        before = path.read_bytes()
        response = await authenticated_client.post(
            endpoint + "/write",
            json={"review_key": preview.json()["review_key"]},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 202, response.text
        assert path.read_bytes() == before
        await manager.dispatch_next()
        report = await authenticated_client.get(endpoint + "/job")
        assert report.status_code == 200
        assert report.json()["job"]["state"] == "COMPLETED"
        assert report.json()["job"]["percent"] == 100


async def test_stale_approval_and_missing_csrf_do_not_queue(authenticated_client, sec_db, tmp_path):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        endpoint = f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/file-metadata"
        response = await authenticated_client.post(endpoint + "/preview")
        assert response.status_code == 403
        before = path.read_bytes()
        response = await authenticated_client.post(
            endpoint + "/write", json={"review_key": "0" * 64}, headers=csrf(authenticated_client)
        )
        assert response.status_code == 409 and "Preview again" in response.text
        assert path.read_bytes() == before


@pytest.mark.parametrize("action", ["preview", "write"])
async def test_unreadable_archive_has_actionable_error_without_queuing(
    authenticated_client, sec_db, tmp_path, action
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        path.write_bytes(b"Damaged comic archive")
        async with sec_db.begin() as session:
            await session.execute(
                update(LibraryFile)
                .where(LibraryFile.id == plan.target.binding.library_file_id)
                .values(
                    file_size=path.stat().st_size,
                    file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        response = await authenticated_client.post(
            f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}"
            f"/file-metadata/{action}",
            json={"review_key": "a" * 64} if action == "write" else {},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409, response.text
        assert "Replace or repair" in response.text
        assert path.read_bytes() == b"Damaged comic archive"


async def conflicting_summary(factory, path, plan):
    with ZipFile(path, "w") as archive:
        archive.writestr("page.jpg", b"page bytes")
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Number>50-X</Number><Summary>ComicInfo summary</Summary>"
            "<Notes>Keep my personal note</Notes></ComicInfo>",
        )
        archive.writestr(
            "MetronInfo.xml",
            "<MetronInfo><Series><Name>Example</Name></Series><Number>50-X</Number>"
            "<Summary>MetronInfo summary</Summary></MetronInfo>",
        )
    async with factory.begin() as session:
        await session.execute(update(Issue).values(description="Library summary"))
        await session.execute(
            update(LibraryFile)
            .where(LibraryFile.id == plan.target.binding.library_file_id)
            .values(
                file_size=path.stat().st_size,
                file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )
        )


@pytest.mark.parametrize("choice", ["library", "ComicInfo.xml", "MetronInfo.xml"])
async def test_conflicts_require_explicit_choices_and_write_one_coherent_pair(
    authenticated_client, sec_db, tmp_path, monkeypatch, choice
):
    import pullbox.api.v1.issue_metadata_links as api
    import pullbox.utilities.executors.file_metadata as executor_module

    failures = []

    def record_error(exc):
        failures.append((type(exc).__name__, getattr(exc, "code", ""), str(exc)))
        return file_metadata_error(exc)

    monkeypatch.setattr(executor_module, "file_metadata_error", record_error)

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        await conflicting_summary(sec_db, path, plan)
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/file-metadata"
        original = path.read_bytes(), path.stat()
        unresolved = await authenticated_client.post(
            endpoint + "/preview", json={}, headers=csrf(authenticated_client)
        )
        assert unresolved.status_code == 200, unresolved.text
        data = unresolved.json()
        assert not data["ready"] and not data["unchanged"]
        conflict = next(item for item in data["conflicts"] if item["key"] == "issue.description")
        assert conflict["selected"] is None
        assert {item["source"] for item in conflict["options"]} == {
            "library",
            "ComicInfo.xml",
            "MetronInfo.xml",
        }
        rejected = await authenticated_client.post(
            endpoint + "/write",
            json={"review_key": data["review_key"]},
            headers=csrf(authenticated_client),
        )
        assert rejected.status_code == 409
        assert (path.read_bytes(), path.stat()) == original
        choices = {"issue.description": choice}
        reviewed = await authenticated_client.post(
            endpoint + "/preview", json={"choices": choices}, headers=csrf(authenticated_client)
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["ready"]
        assert reviewed.json()["review_key"] != data["review_key"]
        queued = await authenticated_client.post(
            endpoint + "/write",
            json={"review_key": reviewed.json()["review_key"], "choices": choices},
            headers=csrf(authenticated_client),
        )
        assert queued.status_code == 202, queued.text
        await manager.dispatch_next()
        assert not failures, failures
        report = await authenticated_client.get(endpoint + "/job")
        assert report.json()["job"]["state"] == "COMPLETED", report.text
        value = {
            "library": "Library summary",
            "ComicInfo.xml": "ComicInfo summary",
            "MetronInfo.xml": "MetronInfo summary",
        }[choice]
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            ci, mi = (
                ET.fromstring(archive.read(name)) for name in ("ComicInfo.xml", "MetronInfo.xml")
            )
            assert ci.findtext("Summary") == mi.findtext("Summary") == value
            assert "Keep my personal note" in ci.findtext("Notes")
        async with sec_db() as session:
            assert (await session.get(Issue, issue_id)).description == value
        again = await authenticated_client.post(
            endpoint + "/preview", json={}, headers=csrf(authenticated_client)
        )
        assert again.status_code == 200 and again.json()["unchanged"]


@pytest.mark.parametrize(
    "choices",
    [
        {"issue.issue_number_text": "library"},
        {"issue.description": "Client supplied text"},
        {"issue.description": {"value": "Client supplied text"}},
        {"identities": "library"},
    ],
)
async def test_review_cannot_supply_values_or_override_identity_fields(
    authenticated_client, choices
):
    response = await authenticated_client.post(
        "/api/v1/issues/1/file-metadata/preview",
        json={"choices": choices},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 422


async def test_approval_cannot_change_or_drop_reviewed_choices(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        await conflicting_summary(sec_db, path, plan)
        endpoint = f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/file-metadata"
        response = await authenticated_client.post(
            endpoint + "/preview",
            json={"choices": {"issue.description": "library"}},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 200 and response.json()["ready"]
        original = path.read_bytes(), path.stat()
        for choices in ({}, {"issue.description": "ComicInfo.xml"}):
            rejected = await authenticated_client.post(
                endpoint + "/write",
                json={"review_key": response.json()["review_key"], "choices": choices},
                headers=csrf(authenticated_client),
            )
            assert rejected.status_code == 409
        assert (path.read_bytes(), path.stat()) == original


async def test_stale_file_count_can_only_be_corrected_to_library_catalog(
    authenticated_client, sec_db, tmp_path
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr(
                "ComicInfo.xml", "<ComicInfo><Number>50-X</Number><Count>999</Count></ComicInfo>"
            )
        async with sec_db.begin() as session:
            await session.execute(
                update(LibraryFile).values(
                    file_size=path.stat().st_size,
                    file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        issue_id = plan.target.binding.metadata.issues[0].local_id
        endpoint = f"/api/v1/issues/{issue_id}/file-metadata/preview"
        result = await authenticated_client.post(
            endpoint, json={}, headers=csrf(authenticated_client)
        )
        assert result.status_code == 200, result.text
        conflict = result.json()["conflicts"][0]
        assert conflict["key"] == "series.issue_count"
        assert [item["source"] for item in conflict["options"] if item["selectable"]] == ["library"]
        bad = await authenticated_client.post(
            endpoint,
            json={"choices": {"series.issue_count": "ComicInfo.xml"}},
            headers=csrf(authenticated_client),
        )
        assert bad.status_code == 409
        good = await authenticated_client.post(
            endpoint,
            json={"choices": {"series.issue_count": "library"}},
            headers=csrf(authenticated_client),
        )
        assert good.status_code == 200 and good.json()["ready"]
        assert any(
            item["field"].endswith("Count[1]")
            and item["after"] == str(plan.series.values.issue_count)
            for item in good.json()["changes"]
        )


@pytest.mark.parametrize(
    "comicinfo,metroninfo",
    [
        ("<ComicInfo><Number>51</Number><Summary>File summary</Summary></ComicInfo>", None),
        (
            "<ComicInfo><Number>50-X</Number>"
            "<Web>https://comicvine.gamespot.com/issue/4000-999/</Web>"
            "<Summary>File summary</Summary></ComicInfo>",
            None,
        ),
        (
            "<ComicInfo><Number>50-X</Number><Summary>File summary</Summary></ComicInfo>",
            "<MetronInfo><Number>50-X</Number></MetronInfo>",
        ),
        (
            "<ComicInfo><Number>50-X</Number><Summary>File summary</Summary>"
            "<Custom>Unsupported</Custom></ComicInfo>",
            None,
        ),
    ],
)
async def test_choices_never_override_number_identity_or_xml_safety(
    authenticated_client, sec_db, tmp_path, comicinfo, metroninfo
):
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr("ComicInfo.xml", comicinfo)
            if metroninfo:
                archive.writestr("MetronInfo.xml", metroninfo)
        async with sec_db.begin() as session:
            await session.execute(update(Issue).values(description="Library summary"))
            await session.execute(
                update(LibraryFile).values(
                    file_size=path.stat().st_size,
                    file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        original = path.read_bytes(), path.stat()
        response = await authenticated_client.post(
            f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/file-metadata/preview",
            json={"choices": {"issue.description": "library"}},
            headers=csrf(authenticated_client),
        )
        assert response.status_code == 409, response.text
        assert (path.read_bytes(), path.stat()) == original


async def test_explicit_not_set_credits_clears_both_documents(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    import pullbox.api.v1.issue_metadata_links as api
    import pullbox.utilities.executors.file_metadata as executor_module

    failures = []

    def record_error(exc):
        failures.append((type(exc).__name__, getattr(exc, "code", ""), str(exc)))
        return file_metadata_error(exc)

    monkeypatch.setattr(executor_module, "file_metadata_error", record_error)

    manager = JobQueueManager(sec_db)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(api, "_get_manager", lambda: manager)
    monkeypatch.setattr(api, "_schedule_dispatch", lambda manager: None)
    async with prepared(sec_db, tmp_path) as (path, _, plan):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Number>50-X</Number><Writer>First writer</Writer></ComicInfo>",
            )
            archive.writestr(
                "MetronInfo.xml",
                "<MetronInfo><Series><Name>Example</Name></Series><Number>50-X</Number>"
                "<Credits><Credit><Creator>Second writer</Creator>"
                "<Roles><Role>Writer</Role></Roles></Credit></Credits></MetronInfo>",
            )
        async with sec_db.begin() as session:
            await session.execute(
                update(LibraryFile).values(
                    file_size=path.stat().st_size,
                    file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        endpoint = f"/api/v1/issues/{plan.target.binding.metadata.issues[0].local_id}/file-metadata"
        choices = {"issue.credits": "library"}
        response = await authenticated_client.post(
            endpoint + "/preview", json={"choices": choices}, headers=csrf(authenticated_client)
        )
        assert response.status_code == 200, response.text
        assert any(
            item["field"].endswith("Writer[1]") and item["after"] is None
            for item in response.json()["changes"]
        )
        queued = await authenticated_client.post(
            endpoint + "/write",
            json={"review_key": response.json()["review_key"], "choices": choices},
            headers=csrf(authenticated_client),
        )
        assert queued.status_code == 202
        await manager.dispatch_next()
        assert not failures, failures
        assert (await authenticated_client.get(endpoint + "/job")).json()["job"][
            "state"
        ] == "COMPLETED"
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert ET.fromstring(archive.read("ComicInfo.xml")).find("Writer") is None
            assert ET.fromstring(archive.read("MetronInfo.xml")).find("Credits") is None
