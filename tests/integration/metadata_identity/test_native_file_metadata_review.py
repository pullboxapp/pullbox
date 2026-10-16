"""Explicit native review reuses conversion and publication, then resumes downloads."""

import shutil
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import select

from pullbox.models import LibraryFile
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.library_conversion_files import decode_plan
from tests.integration.metadata_identity.test_download_paired_metadata import (
    completed_download,
    drain,
    paired_download_setting,
)
from tests.unit.test_nonzip_metadata_writing import nonzip_archive
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source

assert paired_download_setting


async def native_failed_download(factory, tmp_path, suffix=".cb7"):
    download_id, issue_id, source = await completed_download(factory, tmp_path, conflict=True)
    native = source.with_suffix(suffix)
    with ZipFile(source) as archive:
        nonzip_archive(native, [(name, archive.read(name)) for name in archive.namelist()])
    source.unlink()
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        download.downloaded_path = str(native)
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.state is DownloadState.FAILED and download.imported_at is None
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert file.file_path == download.final_path
    return download_id, issue_id, native, Path(download.final_path)


async def no_control():
    pass


async def no_progress(*_args):
    pass


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize(
    "suffix",
    [
        ".cb7",
        pytest.param(
            ".cbr",
            marks=pytest.mark.skipif(
                not shutil.which("unrar"), reason="Native Docker qualification"
            ),
        ),
    ],
)
async def test_native_conflict_can_be_reviewed_written_and_download_retried(
    identity_probe_db, tmp_path, suffix
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, source, copy = await native_failed_download(factory, tmp_path, suffix)
    original, info = source.read_bytes(), source.stat()
    async with factory() as session:
        preview = (await prepare_file_metadata(session, issue_id)).preview
    assert not preview.ready
    assert preview.converts_to_cbz
    assert [conflict.key for conflict in preview.conflicts] == ["series.issue_count"]
    assert copy.read_bytes() == original
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        approved = await prepare_file_metadata(session, issue_id, choices=choices)
    assert approved.preview.ready
    operation = uuid4()
    assert (
        await write_file_metadata(
            factory,
            issue_id,
            approved.preview.review_key,
            operation,
            limit=100_000_000,
            check_control=no_control,
            progress=no_progress,
            choices=choices,
        )
        == "written"
    )
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert download.state is DownloadState.FAILED and download.imported_at is None
        assert download.final_path == file.file_path == str(copy.with_suffix(".cbz"))
        conversion = await session.scalar(select(LibraryConversion))
        assert conversion.state == "complete" and not conversion.active
        plan = decode_plan(conversion.plan_json)
    assert plan.backup.path.read_bytes() == original
    assert not copy.exists()
    with ZipFile(download.final_path) as archive:
        assert ElementTree.fromstring(archive.read("ComicInfo.xml")).findtext("Count") == "5"
        assert (
            ElementTree.fromstring(archive.read("MetronInfo.xml")).findtext("Series/IssueCount")
            == "5"
        )
        assert archive.read("page.jpg") == b"page bytes"
    output_info = Path(download.final_path).stat()
    assert (
        await write_file_metadata(
            factory,
            issue_id,
            approved.preview.review_key,
            operation,
            limit=100_000_000,
            check_control=no_control,
            progress=no_progress,
            choices=choices,
        )
        == "recovered"
    )
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        download.state = DownloadState.COMPLETED
        download.error_message = None
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.imported_at is not None, download.error_message
    assert Path(download.final_path).stat().st_ino == output_info.st_ino
    assert source.read_bytes() == original
    assert (source.stat().st_ino, source.stat().st_mtime_ns, source.stat().st_mode) == (
        info.st_ino,
        info.st_mtime_ns,
        info.st_mode,
    )


@native_pdf
async def test_explicit_pdf_review_runs_in_existing_metadata_job(identity_probe_db, tmp_path):
    from datetime import UTC, datetime

    from pullbox.models import Issue
    from pullbox.models.library import FileFormat
    from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import JobState, JobType, UtilityJob
    from tests.integration.metadata_identity.test_archive_metadata_binding import seed

    _, factory, _ = identity_probe_db
    file_id, issue_id, _, _, zip_path = await seed(factory, tmp_path)
    source = pdf_source(zip_path.with_suffix(".pdf"))
    zip_path.unlink()
    before = source.read_bytes()
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.file_path, file.file_name, file.file_format = str(source), source.name, FileFormat.PDF
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
        issue = await session.get(Issue, issue_id)
        issue.page_count = 3
        session.add(
            SystemConfig(
                key="utility_trash_folder", value=str(tmp_path / "trash"), value_type="string"
            )
        )
    async with factory() as session:
        preview = (await prepare_file_metadata(session, issue_id)).preview
    assert preview.converts_to_cbz and preview.ready
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    async with factory.begin() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            "Review PDF",
            {"issue_id": issue_id, "review_key": preview.review_key},
        )
        job_id = job.id
    await manager.dispatch_next()
    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.COMPLETED, job.error_message
        plan = decode_plan((await session.scalar(select(LibraryConversion))).plan_json)
    assert plan.backup.path.read_bytes() == before
    with ZipFile(source.with_suffix(".cbz")) as archive:
        assert [name for name in archive.namelist() if name.endswith(".jpg")] == [
            f"page_{i:04}.jpg" for i in range(3)
        ]
        assert "MetronInfo.xml" in archive.namelist()


async def test_native_preview_preserves_retained_import_evidence(identity_probe_db, tmp_path):
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSourceType,
    )
    from tests.integration.metadata_identity.test_library_paired_conversion import registered_cb7

    _, factory, _ = identity_probe_db
    source, file_id, issue_id, _, _ = await registered_cb7(factory, tmp_path)
    before = source.read_bytes()
    async with factory.begin() as session:
        job = ImportJob(
            source_type=ImportSourceType.FILESYSTEM,
            source_path=str(tmp_path),
            status=ImportJobStatus.COMPLETED,
        )
        session.add(job)
        await session.flush()
        group = ImportedSeries(import_job_id=job.id, raw_series_name="Canonical series")
        session.add(group)
        await session.flush()
        session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=group.id,
                library_file_id=file_id,
                file_path=str(source),
                file_name=source.name,
                file_format="cb7",
            )
        )
    async with factory() as session:
        with pytest.raises(ValueError, match="import_rollback_protected"):
            await prepare_file_metadata(session, issue_id)
    assert source.read_bytes() == before
    assert not source.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_download_setting")
async def test_stale_native_approval_does_not_convert(identity_probe_db, tmp_path):
    from pullbox.models import Series

    _, factory, _ = identity_probe_db
    _, issue_id, source, copy = await native_failed_download(factory, tmp_path)
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        approved = await prepare_file_metadata(session, issue_id, choices=choices)
    async with factory.begin() as session:
        series = await session.scalar(select(Series))
        series.issue_count = 6
    with pytest.raises(ValueError, match="approval_changed"):
        await write_file_metadata(
            factory,
            issue_id,
            approved.preview.review_key,
            uuid4(),
            limit=100_000_000,
            check_control=no_control,
            progress=no_progress,
            choices=choices,
        )
    assert copy.read_bytes() == source.read_bytes()
    assert not copy.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_download_setting")
async def test_cancel_after_native_intent_releases_reservation(
    identity_probe_db, tmp_path, monkeypatch
):
    from pullbox.core.exceptions import JobCancelledError
    from pullbox.services import library_convert_service

    _, factory, _ = identity_probe_db
    _, issue_id, source, copy = await native_failed_download(factory, tmp_path)
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        prepared = await prepare_file_metadata(session, issue_id, choices=choices)
    cancelled = False
    record = library_convert_service.record_conversion

    async def record_then_cancel(*args, **kwargs):
        nonlocal cancelled
        await record(*args, **kwargs)
        cancelled = True

    async def check():
        if cancelled:
            raise JobCancelledError("stop after intent")

    monkeypatch.setattr(library_convert_service, "record_conversion", record_then_cancel)
    with pytest.raises(JobCancelledError):
        await write_file_metadata(
            factory,
            issue_id,
            prepared.preview.review_key,
            uuid4(),
            limit=100_000_000,
            check_control=check,
            progress=no_progress,
            choices=choices,
        )
    async with factory() as session:
        row = await session.scalar(select(LibraryConversion))
        assert not row.active, "Cancelled review reserved the file until restart"
        assert row.state == "abandoned"
    assert copy.read_bytes() == source.read_bytes()
    assert not copy.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("tamper", [False, True])
async def test_restart_settles_only_proven_native_conversion(
    identity_probe_db, tmp_path, monkeypatch, tamper
):
    import json

    from pullbox.services import native_file_metadata
    from pullbox.services.issue_file_metadata_recovery import recover_file_metadata_jobs
    from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem

    _, factory, _ = identity_probe_db
    download_id, issue_id, source, copy = await native_failed_download(factory, tmp_path)
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        preview = (await prepare_file_metadata(session, issue_id, choices=choices)).preview
    operation, job_id = uuid4(), uuid4().hex
    async with factory.begin() as session:
        session.add(
            UtilityJob(
                id=job_id,
                job_type=JobType.FILE_METADATA,
                display_name="Interrupted native metadata",
                state=JobState.RUNNING,
                total_items=1,
                config=json.dumps(
                    {"issue_id": issue_id, "review_key": preview.review_key, "choices": choices}
                ),
            )
        )
        await session.flush()
        session.add(
            UtilityJobItem(
                id=operation.hex,
                job_id=job_id,
                state=ItemState.IN_PROGRESS,
                item_index=0,
                operation="file_metadata",
            )
        )
    verify = native_file_metadata._verify_conversion

    async def lost_ack(*_args):
        raise RuntimeError("lost conversion acknowledgement")

    monkeypatch.setattr(native_file_metadata, "_verify_conversion", lost_ack)
    with pytest.raises(RuntimeError, match="lost conversion acknowledgement"):
        await write_file_metadata(
            factory,
            issue_id,
            preview.review_key,
            operation,
            limit=100_000_000,
            check_control=no_control,
            progress=no_progress,
            choices=choices,
        )
    monkeypatch.setattr(native_file_metadata, "_verify_conversion", verify)
    output = copy.with_suffix(".cbz")
    if tamper:
        output.write_bytes(output.read_bytes() + b"changed output")
    await recover_file_metadata_jobs(factory)
    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        item = await session.get(UtilityJobItem, operation.hex)
        download = await session.get(DownloadHistory, download_id)
        if tamper:
            assert job.state == JobState.FAILED
            assert download.final_path == str(copy)
        else:
            assert job.state == JobState.COMPLETED
            assert item.state == ItemState.COMPLETED
            assert download.final_path == str(output)
    assert source.exists()


@pytest.mark.usefixtures("paired_download_setting")
async def test_native_review_can_keep_file_description_and_cancel_before_publish(
    identity_probe_db, tmp_path
):
    from datetime import UTC, datetime

    from pullbox.core.exceptions import JobCancelledError
    from pullbox.models import Issue
    from pullbox.models.library import FileFormat
    from tests.integration.metadata_identity.test_library_paired_conversion import registered_cb7

    _, factory, _ = identity_probe_db
    source, file_id, issue_id, _, _ = await registered_cb7(factory, tmp_path)
    async with factory.begin() as session:
        issue = await session.get(Issue, issue_id)
        issue.description = "Different library summary"
        session.add(
            SystemConfig(
                key="utility_trash_folder", value=str(tmp_path / "trash"), value_type="string"
            )
        )
    choices = {"issue.description": "ComicInfo.xml"}
    async with factory() as session:
        prepared = await prepare_file_metadata(session, issue_id, choices=choices)
    before = source.read_bytes()
    cancelled = False

    async def stop():
        if cancelled:
            raise JobCancelledError("stop")

    async def progress(*_args):
        nonlocal cancelled
        cancelled = True

    with pytest.raises(JobCancelledError):
        await write_file_metadata(
            factory,
            issue_id,
            prepared.preview.review_key,
            uuid4(),
            limit=100_000_000,
            check_control=stop,
            progress=progress,
            choices=choices,
        )
    assert source.read_bytes() == before
    assert not source.with_suffix(".cbz").exists()
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_format is FileFormat.CB7
        assert file.file_modified_at == datetime.fromtimestamp(source.stat().st_mtime, UTC)
    await write_file_metadata(
        factory,
        issue_id,
        prepared.preview.review_key,
        uuid4(),
        limit=100_000_000,
        check_control=no_control,
        progress=no_progress,
        choices=choices,
    )
    async with factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue.description == "My local summary"
    with ZipFile(source.with_suffix(".cbz")) as archive:
        for document in ("ComicInfo.xml", "MetronInfo.xml"):
            assert (
                ElementTree.fromstring(archive.read(document)).findtext("Summary")
                == "My local summary"
            )
