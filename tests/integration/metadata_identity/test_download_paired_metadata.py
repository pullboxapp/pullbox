"""Completed downloads use the verified pair, not a ComicInfo-only overwrite."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import delete, select

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.services import issue_file_metadata
from pullbox.tasks import (
    download_post_processing_metadata,
    download_post_processing_queue,
    download_task,
)
from tests.integration.metadata_identity.test_archive_metadata_binding import seed
from tests.unit.test_nonzip_metadata_writing import nonzip_archive
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source


@pytest.fixture
def paired_download_setting(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_DOWNLOAD_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(download_post_processing_queue, "request_story_arc_sync_now", lambda: None)
    # Phase dispatch normally uses the app's global DB factory; this disposable
    # queue test persists terminal progress through its own real session instead.
    monkeypatch.setattr(download_task, "queue_post_processing_phase", lambda *args: None)
    monkeypatch.setattr(download_task, "queue_post_processing_snapshot", lambda *args: None)
    try:
        yield
    finally:
        get_settings.cache_clear()


async def completed_download(factory, tmp_path, *, conflict=False, method="copy", torrent=False):
    file_id, issue_id, series_id, root_id, path = await seed(factory, tmp_path)
    source = tmp_path / "downloads" / "Canonical series 050-X.cbz"
    source.parent.mkdir()
    path.rename(source)
    if conflict:
        with ZipFile(source, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Canonical series</Series><Number>50-X</Number>"
                "<Count>4</Count></ComicInfo>",
            )
    async with factory.begin() as session:
        await session.execute(delete(LibraryFile).where(LibraryFile.id == file_id))
        series = await session.get(Series, series_id)
        series.preferred_library_root_id = root_id
        if conflict:
            series.issue_count = 5
        issue = await session.get(Issue, issue_id)
        issue.status = IssueStatus.DOWNLOADING
        session.add_all(
            SystemConfig(key=key, value=value, value_type=kind)
            for key, value, kind in (
                ("comics_directory", str(tmp_path / "comics"), "string"),
                ("post_processing_method", method, "string"),
                ("torrent_import_strategy", "seed_safe" if torrent else "standard", "string"),
                ("update_embedded_comicinfo_from_match_on_import", "true", "bool"),
                ("convert_to_preferred_format_on_import", "true", "bool"),
                ("skip_existing_files", "true", "bool"),
                ("utility_trash_folder", str(tmp_path / "trash"), "string"),
            )
        )
        download = DownloadHistory(
            issue_id=issue_id,
            title=source.name,
            download_url="test://paired-download",
            download_client=DownloadClientType.QBITTORRENT
            if torrent
            else DownloadClientType.DIRECT,
            state=DownloadState.COMPLETED,
            completed_at=datetime.now(UTC),
            downloaded_path=str(source),
        )
        session.add(download)
        await session.flush()
        return download.id, issue_id, source


async def drain(factory):
    async def run(session, download):
        await download_task._run_post_processing(session, download, cleanup_source=False)

    await download_post_processing_queue.process_completed(run, session_factory=factory)


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("method,torrent", [("copy", False), ("move", False), ("hardlink", True)])
@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
async def test_download_completion_publishes_reconciled_pair_and_preserves_source(
    identity_probe_db, tmp_path, method, torrent, namespace
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(
        factory, tmp_path, method=method, torrent=torrent
    )
    async with factory.begin() as session:
        for model in (SeriesExternalIdentity, IssueExternalIdentity):
            identity = await session.scalar(select(model))
            identity.identity_namespace = namespace
    original, info = source.read_bytes(), source.stat()
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert download.imported_at is not None, download.error_message
        assert download.state is DownloadState.COMPLETED
        assert download.final_path == file.file_path
    with ZipFile(download.final_path) as archive:
        assert "MetronInfo.xml" in archive.namelist(), "Downloads still write ComicInfo only"
        ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        mi_bytes = archive.read("MetronInfo.xml")
        validate_metroninfo_xml(mi_bytes)
        mi = ElementTree.fromstring(mi_bytes)
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        assert ci.findtext("Summary") == mi.findtext("Summary") == "My local summary"
        assert archive.read("page.jpg") == b"page bytes"
    assert file.has_comicinfo
    assert source.read_bytes() == original
    assert (source.stat().st_ino, source.stat().st_mtime_ns, source.stat().st_mode) == (
        info.st_ino,
        info.st_mtime_ns,
        info.st_mode,
    )


@pytest.mark.usefixtures("paired_download_setting")
async def test_conflicting_download_is_not_reported_as_imported_or_overwritten(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(factory, tmp_path, conflict=True)
    original = source.read_bytes()
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert download.imported_at is None, "A genuine Count conflict was silently overwritten"
        assert download.state is DownloadState.FAILED
        assert "review" in download.error_message.lower()
        assert download.final_path == file.file_path
        assert Path(file.file_path).read_bytes() == original
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize(
    "suffix",
    [
        ".cb7",
        pytest.param(
            ".cbr",
            marks=pytest.mark.skipif(
                not shutil.which("unrar"), reason="Native UnRAR qualification runs in Docker"
            ),
        ),
    ],
)
async def test_nonzip_download_uses_paired_conversion_and_preserves_seed_original(
    identity_probe_db, tmp_path, suffix
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, zip_source = await completed_download(factory, tmp_path, torrent=True)
    source = zip_source.with_suffix(suffix)
    with ZipFile(zip_source) as archive:
        nonzip_archive(source, [(name, archive.read(name)) for name in archive.namelist()])
    zip_source.unlink()
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        download.downloaded_path = str(source)
    original = source.read_bytes()
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert download.imported_at is not None, download.error_message
        assert download.final_path == file.file_path
        assert Path(download.final_path).suffix == ".cbz"
    with ZipFile(download.final_path) as archive:
        assert "MetronInfo.xml" in archive.namelist()
        assert archive.read("page.jpg") == b"page bytes"
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
async def test_import_rollback_owned_file_cannot_be_replaced_by_the_download(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    await drain(factory)
    async with factory.begin() as session:
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        path = Path(file.file_path)
        before = path.read_bytes()
        job = ImportJob(
            source_path=str(source.parent),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
        )
        session.add(job)
        await session.flush()
        series = ImportedSeries(import_job_id=job.id, raw_series_name="Canonical series")
        session.add(series)
        await session.flush()
        session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path=str(path),
                file_name=path.name,
                file_format="cbz",
                library_file_id=file.id,
            )
        )
        download = await session.get(DownloadHistory, download_id)
        download.state = DownloadState.COMPLETED
        download.imported_at = None
        download.final_path = None
        download.replace_existing_file = True
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.state is DownloadState.FAILED and download.imported_at is None
        assert "protected by import rollback history" in download.error_message
    assert path.read_bytes() == before
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("remove_original", [False, True])
async def test_retry_resumes_the_reviewed_library_copy_without_retransferring(
    identity_probe_db, tmp_path, monkeypatch, remove_original
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(factory, tmp_path, conflict=True)
    original = source.read_bytes()
    await drain(factory)
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        path = Path(file.file_path)
        # Simulate the user's explicit library-copy repair, never the download original.
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Canonical series</Series><Number>50-X</Number>"
                "<Count>5</Count></ComicInfo>",
            )
        file.file_size = path.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        download.state = DownloadState.COMPLETED

    async def never_transfer(*args, **kwargs):
        pytest.fail("Retry must use the registered managed copy, not transfer the release again")

    monkeypatch.setattr(download_task, "transfer_and_register_library_file", never_transfer)
    if remove_original:
        source.unlink()
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.imported_at is not None, download.error_message
        assert download.error_message is None
    with ZipFile(download.final_path) as archive:
        assert "MetronInfo.xml" in archive.namelist()
    if not remove_original:
        assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
async def test_lost_write_acknowledgement_is_recovered_without_rewriting(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    download_id, _, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    real_write = download_post_processing_metadata.write_file_metadata

    async def lose_acknowledgement(*args, **kwargs):
        await real_write(*args, **kwargs)
        raise OSError("Simulated lost completion acknowledgement")

    monkeypatch.setattr(
        download_post_processing_metadata, "write_file_metadata", lose_acknowledgement
    )
    await drain(factory)
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.state is DownloadState.FAILED and download.imported_at is None
        path = Path(download.final_path)
        before = path.read_bytes(), path.stat().st_mtime_ns
        download.state = DownloadState.COMPLETED

    async def never_rewrite(*args, **kwargs):
        pytest.fail("Recovery must inspect the current pair, not write the completed output again")

    monkeypatch.setattr(download_post_processing_metadata, "write_file_metadata", never_rewrite)
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.imported_at is not None, download.error_message
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
async def test_provider_identity_change_during_staging_rejects_publication(
    identity_probe_db, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    _, factory, _ = identity_probe_db
    download_id, _, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    stage = issue_file_metadata.stage_cbz_metadata_interruptible

    @asynccontextmanager
    async def change_identity(*args, **kwargs):
        async with stage(*args, **kwargs) as result:
            async with factory.begin() as session:
                identity = await session.scalar(select(IssueExternalIdentity))
                identity.verification_state = IdentityVerificationState.CONFLICTED
                identity.revision += 1
            yield result

    monkeypatch.setattr(issue_file_metadata, "stage_cbz_metadata_interruptible", change_identity)
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.imported_at is None and download.state is DownloadState.FAILED
        assert Path(download.final_path).read_bytes() == original
    assert source.read_bytes() == original


@native_pdf
@pytest.mark.usefixtures("paired_download_setting")
async def test_pdf_download_uses_paired_conversion_without_changing_the_pdf(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    download_id, issue_id, zip_source = await completed_download(factory, tmp_path)
    source = pdf_source(zip_source.with_suffix(".pdf"))
    zip_source.unlink()
    async with factory.begin() as session:
        download = await session.get(DownloadHistory, download_id)
        download.downloaded_path = str(source)
        issue = await session.get(Issue, issue_id)
        issue.page_count = 3
    original = source.read_bytes()
    await drain(factory)
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download.imported_at is not None, download.error_message
    with ZipFile(download.final_path) as archive:
        ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        assert ci.findtext("PageCount") == mi.findtext("PageCount") == "3"
        assert [name for name in archive.namelist() if name.endswith(".jpg")] == [
            "page_0000.jpg",
            "page_0001.jpg",
            "page_0002.jpg",
        ]
    assert source.read_bytes() == original


async def test_disabled_flag_preserves_legacy_download_behavior(
    identity_probe_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_DOWNLOAD_WRITER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        _, factory, _ = identity_probe_db
        download_id, _, _ = await completed_download(factory, tmp_path)
        await drain(factory)
        async with factory() as session:
            download = await session.get(DownloadHistory, download_id)
            assert download.imported_at is not None, download.error_message
        with ZipFile(download.final_path) as archive:
            assert "ComicInfo.xml" in archive.namelist()
            assert "MetronInfo.xml" not in archive.namelist()
    finally:
        get_settings.cache_clear()
