"""Direct acquisitions retain their own lifecycle while writing reconciled metadata."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import delete, select

from pullbox.config import get_settings
from pullbox.models.config import SystemConfig
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactState,
)
from pullbox.models.download import DownloadHistory
from pullbox.models.library import LibraryFile
from pullbox.models.pending_match import PendingMatch
from pullbox.services import direct_acquisition_executor, direct_paired_metadata
from pullbox.services.direct_artifact_post_processing import run_direct_artifact_post_processing
from pullbox.services.direct_artifact_quarantine import DirectArtifactQuarantine
from pullbox.services.intervention_service import InterventionService
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from tests.integration.metadata_identity.test_download_paired_metadata import (
    completed_download,
    paired_download_setting,  # noqa: F401
)
from tests.unit.test_direct_acquisition_executor import (
    _attempt,
    _executor,
    _UnexpectedTransport,
)
from tests.unit.test_nonzip_metadata_writing import nonzip_archive
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source


@pytest.fixture(autouse=True)
def no_global_sync(monkeypatch):
    monkeypatch.setattr(direct_acquisition_executor, "request_story_arc_sync_now", lambda: None)
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_DIRECT_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def direct_artifact(factory, tmp_path, *, conflict=False, suffix=".cbz", large=False):
    if suffix == ".cbr" and not shutil.which("unrar"):
        pytest.skip("Native direct RAR qualification requires UnRAR")
    download_id, issue_id, source = await completed_download(factory, tmp_path, conflict=conflict)
    async with factory() as session:
        await session.execute(delete(DownloadHistory).where(DownloadHistory.id == download_id))
        attempt = _attempt()
        attempt.issue_id = issue_id
        attempt.state = (
            DirectAcquisitionState.VALIDATING if large else DirectAcquisitionState.POST_PROCESSING
        )
        artifact = attempt.artifact_attempts[0]
        artifact.state = DirectArtifactState.VALIDATING
        session.add(attempt)
        await session.commit()
        workspace = DirectArtifactQuarantine(tmp_path / "quarantine").prepare(
            acquisition_id=attempt.id, artifact_id=artifact.id
        )
        quarantined = workspace.directory / f"artifact-{artifact.id}{suffix}"
        with ZipFile(source) as archive:
            members = [(name, archive.read(name)) for name in archive.namelist()]
        if large:
            members = [
                (name, payload * 220000 if name == "page.jpg" else payload)
                for name, payload in members
            ]
            session.add(SystemConfig(key="archive_size_limit_mb", value="1", value_type="int"))
        if suffix == ".cbz":
            with ZipFile(quarantined, "w") as archive:
                for name, payload in members:
                    archive.writestr(name, payload)
        else:
            nonzip_archive(quarantined, members)
        artifact.quarantine_path = str(quarantined)
        await session.commit()
        return attempt.id, artifact.id, issue_id, quarantined


async def execute(factory, tmp_path, attempt_id, artifact_id, *, cancel_event=None):
    async def no_download():
        pytest.fail("A completed artifact must not be downloaded again")

    async with factory() as session:
        return await _executor(
            tmp_path,
            transport=_UnexpectedTransport(),
            post_processor=run_direct_artifact_post_processing,
        ).execute(
            session,
            acquisition_id=attempt_id,
            artifact_id=artifact_id,
            source_factory=no_download,
            cancel_event=cancel_event,
        )


async def approve_size(factory):
    async with factory() as session:
        pending = await session.scalar(select(PendingMatch))
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        await InterventionService(direct_runner_getter=lambda: runner).approve_match(
            session, pending.id
        )


@pytest.mark.usefixtures("paired_download_setting")
async def test_direct_size_approval_rejects_changed_quarantine(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, large=True)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    await approve_size(factory)
    with ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"changed page" * 220000)
    changed = source.read_bytes()
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert source.read_bytes() == changed
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert "changed" in (await session.scalar(select(DownloadHistory))).error_message


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7", ".cbr"])
@pytest.mark.parametrize("large", [False, True])
async def test_direct_executor_completes_only_after_reconciled_writing(
    identity_probe_db, tmp_path, suffix, large
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(
        factory, tmp_path, suffix=suffix, large=large
    )
    if large:
        blocked = await execute(factory, tmp_path, attempt_id, artifact_id)
        assert blocked.state is DirectAcquisitionState.INTERVENTION
        await approve_size(factory)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        assert history.imported_at is not None
        with ZipFile(history.final_path) as archive:
            assert "MetronInfo.xml" in archive.namelist(), "Direct acquisition still writes CI only"
            ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
            mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
            assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
            assert archive.read("page.jpg") == b"page bytes" * (220000 if large else 1)
        if large:
            assert (await session.get(SystemConfig, "archive_size_limit_mb")).value == "1"
    assert not source.exists(), "Normal quarantine cleanup follows verified completion"


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7"])
@pytest.mark.parametrize("large", [False, True])
async def test_direct_conflict_keeps_copy_for_review_and_retry(
    identity_probe_db, tmp_path, suffix, large
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_id, source = await direct_artifact(
        factory, tmp_path, conflict=True, suffix=suffix, large=large
    )
    original = source.read_bytes()
    if large:
        assert (
            await execute(factory, tmp_path, attempt_id, artifact_id)
        ).state is DirectAcquisitionState.INTERVENTION
        await approve_size(factory)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        assert history.imported_at is None
        assert "Review file metadata" in history.error_message
        path = Path(history.final_path)
        assert path.read_bytes() == original == source.read_bytes()
        prepared = await prepare_file_metadata(
            session, issue_id, choices={"series.issue_count": "library"}
        )
    assert prepared.preview.ready

    async def control():
        pass

    async def progress(*args):
        pass

    await write_file_metadata(
        factory,
        issue_id,
        prepared.preview.review_key,
        uuid4(),
        limit=(1 if large else 10) * 1024**2,
        choices={"series.issue_count": "library"},
        check_control=control,
        progress=progress,
    )
    path = path.with_suffix(".cbz")
    reviewed = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        assert attempt.library_file_id == prepared.preview.file_id
        assert history.imported_at is not None and history.final_path == str(path)
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == reviewed


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7"])
async def test_direct_lost_acknowledgement_recovers_without_copying_or_rewriting(
    identity_probe_db, tmp_path, monkeypatch, suffix
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, suffix=suffix)
    original = source.read_bytes()
    real_write = direct_paired_metadata.write_file_metadata

    async def lose_ack(*args, **kwargs):
        await real_write(*args, **kwargs)
        raise OSError("Lost acknowledgement after paired publication")

    monkeypatch.setattr(direct_paired_metadata, "write_file_metadata", lose_ack)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    async with factory() as session:
        file = await session.scalar(select(LibraryFile))
        path = Path(file.file_path)
        written = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
    assert source.read_bytes() == original

    async def never_rewrite(*args, **kwargs):
        pytest.fail("A committed pair must not be written twice")

    monkeypatch.setattr(direct_paired_metadata, "write_file_metadata", never_rewrite)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == written


@pytest.mark.usefixtures("paired_download_setting")
async def test_pending_direct_metadata_cannot_fall_back_to_legacy_writer(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, conflict=True)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    original = source.read_bytes()
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_DIRECT_WRITER_ENABLED", "false")
    get_settings.cache_clear()
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        assert history.imported_at is None
        assert Path(history.final_path).read_bytes() == original == source.read_bytes()


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7"])
@pytest.mark.parametrize("after_publication", [False, True])
async def test_direct_cancel_before_metadata_publication_keeps_managed_copy(
    identity_probe_db, tmp_path, monkeypatch, suffix, after_publication
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, suffix=suffix)
    original = source.read_bytes()
    cancel = asyncio.Event()
    real_write = direct_paired_metadata.write_file_metadata

    async def cancel_before_write(*args, **kwargs):
        if after_publication:
            result = await real_write(*args, **kwargs)
            cancel.set()
            return result
        cancel.set()
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(direct_paired_metadata, "write_file_metadata", cancel_before_write)
    result = await execute(factory, tmp_path, attempt_id, artifact_id, cancel_event=cancel)
    assert result.state is (
        DirectAcquisitionState.COMPLETED if after_publication else DirectAcquisitionState.CANCELLED
    )
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        assert (history.imported_at is not None) is after_publication
        file = await session.scalar(select(LibraryFile))
        if after_publication:
            with ZipFile(file.file_path) as archive:
                assert "MetronInfo.xml" in archive.namelist()
        else:
            assert Path(file.file_path).read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("changed", ["copy", "source", "approval"])
async def test_direct_review_does_not_reuse_changed_size_approval(
    identity_probe_db, tmp_path, changed
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_id, source = await direct_artifact(
        factory, tmp_path, conflict=True, large=True
    )
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    await approve_size(factory)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    async with factory.begin() as session:
        file = await session.scalar(select(LibraryFile))
        path = Path(file.file_path)
        if changed == "approval":
            attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
            attempt.plan_snapshot = {**attempt.plan_snapshot, "safety_review": {}}
        else:
            with ZipFile(path if changed == "copy" else source, "a") as archive:
                archive.writestr("added.jpg", b"another page")
    async with factory() as session:
        if changed == "copy":
            from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError

            with pytest.raises(ArchiveMetadataBindingError, match="source_changed"):
                await prepare_file_metadata(session, issue_id)
            return
        prepared = await prepare_file_metadata(
            session, issue_id, choices={"series.issue_count": "library"}
        )
        assert prepared.approved_resource_limit is None


@pytest.mark.usefixtures("paired_download_setting")
@native_pdf
async def test_direct_pdf_uses_existing_paired_conversion(identity_probe_db, tmp_path):
    from pullbox.models.direct_acquisition import DirectArtifactAttempt

    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, zip_source = await direct_artifact(factory, tmp_path)
    source = pdf_source(zip_source.with_suffix(".pdf"))
    zip_source.unlink()
    async with factory.begin() as session:
        artifact = await session.get(DirectArtifactAttempt, artifact_id)
        artifact.quarantine_path = str(source)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        history = await session.scalar(select(DownloadHistory))
        assert history.imported_at is not None
        with ZipFile(history.final_path) as archive:
            assert {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive.namelist())
            assert len([name for name in archive.namelist() if name.endswith(".jpg")]) == 3


@pytest.mark.usefixtures("paired_download_setting")
async def test_direct_size_approval_never_allows_dangerous_payload(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, large=True)
    with ZipFile(source, "a") as archive:
        archive.writestr("payload.exe", b"unsafe payload")
    original = source.read_bytes()
    blocked = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert blocked.state is DirectAcquisitionState.INTERVENTION
    await approve_size(factory)
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert source.read_bytes() == original
    async with factory() as session:
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
