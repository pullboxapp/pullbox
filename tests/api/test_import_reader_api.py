"""Read-only import previews reuse the bounded comic reader."""

import zipfile

import pytest
from sqlalchemy import func, select

from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJob
from pullbox.models.library import LibraryFile
from pullbox.models.reader import IssueReaderState
from pullbox.services.reader_content_service import ReaderContentService
from tests.api.test_reader_api import _write_cbz
from tests.ui.test_import_one_page_review import _seed_one_page_job

pytest_plugins = ["tests.conftest_security"]


async def _preview_fixture(sec_db, sec_app, tmp_path):
    seeded = await _seed_one_page_job(sec_db, 1)
    root = tmp_path / "source"
    root.mkdir()
    source = root / "cover.cbz"
    _write_cbz(source, page_count=1)
    async with sec_db() as session:
        (await session.get(ImportJob, seeded["job_id"])).source_path = str(root)
        file = await session.get(ImportedFile, seeded["file_ids"][0])
        file.file_path = str(source)
        file.file_name = source.name
        file.source_signature = build_file_identity_signature(source)
        await session.commit()
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    url = f"/api/v1/reader/imports/{seeded['job_id']}/files/{seeded['file_ids'][0]}"
    return seeded, source, url


async def test_import_reader_is_read_only(authenticated_client, sec_db, sec_app, tmp_path):
    seeded, source, url = await _preview_fixture(sec_db, sec_app, tmp_path)
    before = source.read_bytes()
    response = await authenticated_client.get(url + "/manifest")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["page_count"] == 1
    assert "progress_url" not in manifest
    assert "completion_url" not in manifest
    assert "download_url" not in manifest
    page = await authenticated_client.get(
        manifest["page_url_template"].replace("{page_index}", "0")
    )
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("image/")
    assert source.read_bytes() == before
    async with sec_db() as session:
        file = await session.get(ImportedFile, seeded["file_ids"][0])
        assert file.status == ImportedFileStatus.SAFETY_BLOCKED
        assert not file.include_in_import
        assert not file.diagnostics.get("safety_exception")
        assert await session.scalar(select(func.count()).select_from(LibraryFile)) == 0
        assert await session.scalar(select(func.count()).select_from(IssueReaderState)) == 0


@pytest.mark.parametrize(
    "problem",
    ["damaged", "image", "traversal", "symlink", "changed", "wrong_job", "dangerous", "missing"],
)
async def test_import_reader_reports_errors_without_approving(
    authenticated_client, sec_db, sec_app, tmp_path, problem
):
    seeded, source, url = await _preview_fixture(sec_db, sec_app, tmp_path)
    if problem == "damaged":
        source.write_bytes(b"PK\x03\x04broken")
    elif problem in {"image", "traversal"}:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../page.gif" if problem == "traversal" else "page.gif", b"broken")
    elif problem == "symlink":
        outside = tmp_path / "outside.cbz"
        _write_cbz(outside, page_count=1)
        source.unlink()
        source.symlink_to(outside)
    elif problem == "changed":
        _write_cbz(source, page_count=2)
    elif problem == "missing":
        source.unlink()
    elif problem == "wrong_job":
        url = url.replace(f"imports/{seeded['job_id']}", "imports/999999")
    async with sec_db() as session:
        file = await session.get(ImportedFile, seeded["file_ids"][0])
        if problem in {"damaged", "image", "traversal", "symlink"}:
            file.source_signature = build_file_identity_signature(source)
        if problem == "dangerous":
            file.diagnostics = {"safety_block": {"category": "dangerous_path_or_payload"}}
        await session.commit()
    response = await authenticated_client.get(url + "/manifest")
    assert response.status_code in {404, 409, 422}
    message = response.json().get("detail", response.json())
    assert "Skip" in str(message) or problem == "wrong_job"
    assert str(tmp_path) not in str(message)
    async with sec_db() as session:
        assert (
            await session.get(ImportedFile, seeded["file_ids"][0])
        ).status == ImportedFileStatus.SAFETY_BLOCKED


async def test_import_reader_requires_login(unauthenticated_client):
    response = await unauthenticated_client.get("/api/v1/reader/imports/1/files/1/manifest")
    assert response.status_code in {401, 403}


async def test_import_preview_revalidates_every_page(
    authenticated_client, sec_db, sec_app, tmp_path
):
    _seeded, source, url = await _preview_fixture(sec_db, sec_app, tmp_path)
    manifest = (await authenticated_client.get(url + "/manifest")).json()
    page_url = manifest["page_url_template"].replace("{page_index}", "0")
    assert (await authenticated_client.get(url + "/pages/0?revision=stale")).status_code == 409
    _write_cbz(source, page_count=2)
    assert (await authenticated_client.get(page_url)).status_code == 409


async def test_import_preview_supports_read_only_mylar_root(
    authenticated_client, sec_db, sec_app, tmp_path
):
    from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType
    from pullbox.models.library import LibraryRoot

    seeded, source, url = await _preview_fixture(sec_db, sec_app, tmp_path)
    async with sec_db() as session:
        root = LibraryRoot(
            name="Mylar",
            path=str(source.parent),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=False,
        )
        session.add(root)
        await session.flush()
        job = await session.get(ImportJob, seeded["job_id"])
        job.source_type = ImportSourceType.MYLAR3
        job.source_path = str(tmp_path / "mylar.db")
        job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
        file = await session.get(ImportedFile, seeded["file_ids"][0])
        file.source_signature = {**file.source_signature, "mylar_reference_root_id": root.id}
        await session.commit()
    assert (await authenticated_client.get(url + "/manifest")).status_code == 200
    async with sec_db() as session:
        (await session.get(LibraryRoot, root.id)).enabled = False
        await session.commit()
    assert (await authenticated_client.get(url + "/manifest")).status_code == 409
