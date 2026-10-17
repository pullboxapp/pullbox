"""Recovery completes retained metadata, not a second acquisition."""

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.exceptions import JobCancelledError
from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import IssueStatus
from pullbox.models.pending_match import PendingMatch
from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.direct_metadata_recovery import (
    prepare_direct_metadata_recovery,
    run_direct_metadata_recovery,
)
from pullbox.services.intervention_service import InterventionService
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    direct_artifact,
    execute,
    no_global_sync,  # noqa: F401
    paired_download_setting,  # noqa: F401
)
from tests.unit.test_nonzip_metadata_writing import nonzip_archive


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize(
    "suffix,remove_copy", [(".cbz", False), (".cb7", False), (".cbz", True), (".cb7", True)]
)
@pytest.mark.parametrize("rating", ["Unknown", "Mature"])
async def test_shared_xml_choices_complete_recovery_without_reacquisition(
    identity_probe_db, tmp_path, suffix, remove_copy, rating
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_id, source = await direct_artifact(
        factory, tmp_path, conflict=True, suffix=suffix
    )
    members = [
        ("page.jpg", b"page bytes"),
        (
            "ComicInfo.xml",
            b"<ComicInfo><Number>50-X</Number><Count>4</Count>"
            b"<AgeRating>Teen</AgeRating><Tags>Original tag</Tags></ComicInfo>",
        ),
        (
            "MetronInfo.xml",
            (
                "<MetronInfo><Series><Name>Canonical series</Name></Series>"
                f"<Number>50-X</Number><AgeRating>{rating}</AgeRating>"
                "<Tags><Tag>Alternate tag</Tag></Tags></MetronInfo>"
            ).encode(),
        ),
    ]
    if suffix == ".cbz":
        with ZipFile(source, "w") as archive:
            for name, value in members:
                archive.writestr(name, value)
    else:
        source.unlink()
        nonzip_archive(source, members)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    async with factory.begin() as session:
        pending = await session.scalar(select(PendingMatch))
        pending_id = pending.id
        file = await session.scalar(select(LibraryFile))
        copy = Path(file.file_path)
        if remove_copy:
            copy.unlink()
            await session.delete(file)
            (await session.get(Issue, issue_id)).status = IssueStatus.WANTED
    original = source.read_bytes()
    async with factory() as session:
        preview = (await prepare_direct_metadata_recovery(session, pending_id)).preview
        expected = {"series.issue_count", "xml.Tags"}
        if rating != "Unknown":
            expected.add("xml.AgeRating")
        assert {c.key for c in preview.conflicts} == expected
        choices = {
            c.key: "ComicInfo.xml" if c.key.startswith("xml.") else "library"
            for c in preview.conflicts
        }
        approved = await prepare_direct_metadata_recovery(session, pending_id, choices=choices)
        assert approved.preview.ready
        with pytest.raises(ArchiveMetadataRenderError, match="invalid_choice"):
            await prepare_direct_metadata_recovery(
                session, pending_id, choices={**choices, "xml.Tags": "library"}
            )
    changed_choices = {**choices, "xml.Tags": "MetronInfo.xml"}
    with pytest.raises(ValueError, match="changed"):
        await run_direct_metadata_recovery(
            factory,
            pending_id,
            approved.preview.review_key,
            uuid4(),
            choices=changed_choices,
            limit=10 * 1024**2,
            check_control=AsyncMock(),
            progress=AsyncMock(),
        )
    assert source.read_bytes() == original
    await run_direct_metadata_recovery(
        factory,
        pending_id,
        approved.preview.review_key,
        uuid4(),
        choices=choices,
        limit=10 * 1024**2,
        check_control=AsyncMock(),
        progress=AsyncMock(),
    )
    async with factory() as session:
        file = await session.scalar(select(LibraryFile))
        with ZipFile(file.file_path) as archive:
            ci = ET.fromstring(archive.read("ComicInfo.xml"))
            mi = ET.fromstring(archive.read("MetronInfo.xml"))
            assert ci.findtext("AgeRating") == mi.findtext("AgeRating") == "Teen"
            assert ci.findtext("Tags") == mi.findtext("Tags/Tag") == "Original tag"
            assert archive.read("page.jpg") == b"page bytes"
        assert (await session.get(PendingMatch, pending_id)).status == "approved"
        assert (
            await session.get(DirectAcquisitionAttempt, attempt_id)
        ).state is DirectAcquisitionState.COMPLETED
        assert (await session.scalar(select(DownloadHistory))).state is DownloadState.IMPORTED
        assert await session.scalar(select(func.count()).select_from(DirectAcquisitionAttempt)) == 1
    assert source.read_bytes() == original


async def _review_case(factory, tmp_path, *, suffix=".cbz", remove_copy=False):
    attempt_id, artifact_id, issue_id, source = await direct_artifact(
        factory, tmp_path, conflict=True, suffix=suffix
    )
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    async with factory.begin() as session:
        pending = await session.scalar(select(PendingMatch))
        file = await session.scalar(select(LibraryFile))
        if remove_copy:
            Path(file.file_path).unlink()
            await session.delete(file)
            issue = await session.get(Issue, issue_id)
            issue.status = IssueStatus.WANTED
        return pending.id, attempt_id, issue_id, source


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize(
    "suffix,remove_copy",
    [(".cbz", False), (".cb7", False), (".cbz", True), (".cb7", True), (".cbr", True)],
)
async def test_review_reuses_copy_or_restores_retained_download(
    identity_probe_db, tmp_path, suffix, remove_copy
):
    _, factory, _ = identity_probe_db
    pending_id, attempt_id, issue_id, source = await _review_case(
        factory, tmp_path, suffix=suffix, remove_copy=remove_copy
    )
    original = source.read_bytes()
    async with factory() as session:
        prepared = await prepare_direct_metadata_recovery(session, pending_id)
        assert prepared is not None, "Recovery has no retained-file metadata review"
        assert not prepared.preview.ready
        assert prepared.preview.restores_copy is remove_copy
        assert prepared.preview.conflicts[0].key == "series.issue_count"
        prepared = await prepare_direct_metadata_recovery(
            session, pending_id, choices={"series.issue_count": "library"}
        )
    assert prepared.preview.ready
    operation = uuid4()
    progress = AsyncMock()
    await run_direct_metadata_recovery(
        factory,
        pending_id,
        prepared.preview.review_key,
        operation,
        choices={"series.issue_count": "library"},
        limit=10 * 1024**2,
        check_control=AsyncMock(),
        progress=progress,
    )
    assert progress.await_count > 0
    async with factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        pending = await session.get(PendingMatch, pending_id)
        history = await session.scalar(select(DownloadHistory))
        file = await session.scalar(select(LibraryFile))
        assert attempt.state is DirectAcquisitionState.COMPLETED
        assert history.state is DownloadState.IMPORTED and history.imported_at
        assert pending.status == "approved"
        assert pending.match_details["resolution_reason"] == "metadata_completed"
        assert await session.scalar(select(func.count()).select_from(DirectAcquisitionAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(DownloadHistory)) == 1
        assert (await session.get(Issue, issue_id)).status is IssueStatus.OWNED
        with ZipFile(file.file_path) as archive:
            assert "ComicInfo.xml" in archive.namelist()
            assert "MetronInfo.xml" in archive.namelist()
            assert archive.read("page.jpg") == b"page bytes"
        written = Path(file.file_path).read_bytes()
    assert source.read_bytes() == original
    await run_direct_metadata_recovery(
        factory,
        pending_id,
        prepared.preview.review_key,
        operation,
        choices={"series.issue_count": "library"},
        limit=10 * 1024**2,
        check_control=AsyncMock(),
        progress=AsyncMock(),
    )
    async with factory() as session:
        assert Path((await session.scalar(select(LibraryFile))).file_path).read_bytes() == written


@pytest.mark.usefixtures("paired_download_setting")
async def test_generic_route_retry_never_redownloads_metadata_failure(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    pending_id, _, _, _ = await _review_case(factory, tmp_path)
    planner = AsyncMock(side_effect=AssertionError("Metadata recovery attempted network planning"))
    async with factory() as session:
        with pytest.raises(ValueError, match="metadata"):
            await InterventionService(direct_planner=planner).retry_direct_recovery(
                session, pending_id
            )
    planner.assert_not_called()


@pytest.mark.usefixtures("paired_download_setting")
async def test_metadata_recovery_runs_in_existing_utility_queue(identity_probe_db, tmp_path):
    from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import JobState, JobType, UtilityJob

    _, factory, _ = identity_probe_db
    pending_id, _, issue_id, source = await _review_case(factory, tmp_path)
    original = source.read_bytes()
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        prepared = await prepare_direct_metadata_recovery(session, pending_id, choices=choices)
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    async with factory.begin() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            "Recover metadata",
            {
                "issue_id": issue_id,
                "review_key": prepared.preview.review_key,
                "choices": choices,
                "recovery_pending_id": pending_id,
            },
        )
        job_id = job.id
    await manager.dispatch_next()
    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.COMPLETED, job.error_message
        assert (await session.get(PendingMatch, pending_id)).status == "approved"
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("change", ["source", "metadata", "cancel", "reject"])
async def test_changed_recovery_or_cancel_preserves_both_copies(
    identity_probe_db, tmp_path, change
):
    _, factory, _ = identity_probe_db
    pending_id, _, issue_id, source = await _review_case(factory, tmp_path)
    async with factory() as session:
        prepared = await prepare_direct_metadata_recovery(
            session, pending_id, choices={"series.issue_count": "library"}
        )
        file = await session.scalar(select(LibraryFile))
        path = Path(file.file_path)
    if change == "source":
        source.touch()
    elif change == "metadata":
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).title = "Changed title"
    elif change == "reject":
        async with factory.begin() as session:
            (await session.get(PendingMatch, pending_id)).status = "rejected"
    originals = source.read_bytes(), path.read_bytes()
    control = (
        AsyncMock(side_effect=JobCancelledError("Cancelled")) if change == "cancel" else AsyncMock()
    )
    with pytest.raises((ValueError, JobCancelledError)):
        await run_direct_metadata_recovery(
            factory,
            pending_id,
            prepared.preview.review_key,
            uuid4(),
            choices={"series.issue_count": "library"},
            limit=10 * 1024**2,
            check_control=control,
            progress=AsyncMock(),
        )
    assert (source.read_bytes(), path.read_bytes()) == originals
    async with factory() as session:
        assert (await session.scalar(select(DownloadHistory))).state == DownloadState.FAILED


@pytest.mark.usefixtures("paired_download_setting")
async def test_recovery_api_previews_approval_and_deduplicates_admission(
    identity_probe_db, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from pullbox.api.deps import get_db_dep, require_interactive_auth
    from pullbox.api.v1.router import v1_router
    from pullbox.utilities import router as utilities_router
    from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import JobType, UtilityJob

    _, factory, _ = identity_probe_db
    pending_id, _, _, source = await _review_case(factory, tmp_path)
    original = source.read_bytes()
    app = FastAPI()
    app.include_router(v1_router)

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_dep] = db
    app.dependency_overrides[require_interactive_auth] = lambda: SimpleNamespace(
        username="operator"
    )
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    monkeypatch.setattr(utilities_router, "_queue_manager", manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/v1/intervention/{pending_id}/file-metadata"
        preview = await client.post(url + "/preview", json={})
        assert preview.status_code == 200, preview.text
        assert preview.json()["ready"] is False
        blocked = await client.post(
            url + "/write", json={"review_key": preview.json()["review_key"]}
        )
        assert blocked.status_code == 409
        choices = {"series.issue_count": "library"}
        preview = await client.post(url + "/preview", json={"choices": choices})
        assert preview.status_code == 200
        body = {"review_key": preview.json()["review_key"], "choices": choices}
        # Keep dispatch queued so simultaneous/double clicks must reuse admission.
        from pullbox.api.v1 import intervention_metadata

        monkeypatch.setattr(intervention_metadata, "_get_manager", lambda: manager)
        monkeypatch.setattr(intervention_metadata, "_schedule_dispatch", lambda _: None)
        first = await client.post(url + "/write", json=body)
        second = await client.post(url + "/write", json=body)
        assert first.status_code == second.status_code == 202, (first.text, second.text)
        assert first.json()["job_id"] == second.json()["job_id"]
        job = await client.get(url + "/job")
        assert job.json()["job"]["id"] == first.json()["job_id"]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(UtilityJob)) == 1
        assert (await session.get(PendingMatch, pending_id)).status == "pending"
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7"])
@pytest.mark.parametrize("tamper", [False, True])
async def test_restart_settles_proven_write_and_recovery_together(
    identity_probe_db, tmp_path, monkeypatch, suffix, tamper
):
    from pullbox.services import direct_metadata_recovery as recovery
    from pullbox.services.issue_file_metadata_recovery import recover_file_metadata_jobs
    from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem

    _, factory, _ = identity_probe_db
    pending_id, attempt_id, issue_id, _source = await _review_case(factory, tmp_path, suffix=suffix)
    async with factory() as session:
        prepared = await prepare_direct_metadata_recovery(
            session, pending_id, choices={"series.issue_count": "library"}
        )
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    async with factory.begin() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            "Recovery crash",
            {
                "issue_id": issue_id,
                "recovery_pending_id": pending_id,
                "review_key": prepared.preview.review_key,
                "choices": {"series.issue_count": "library"},
            },
        )
        job_id = job.id
    with monkeypatch.context() as patch:
        patch.setattr(
            recovery, "_complete", AsyncMock(side_effect=RuntimeError("Lost acknowledgement"))
        )
        await manager.dispatch_next()
    async with factory.begin() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.FAILED
        job.state = JobState.RUNNING
        item = await session.scalar(select(UtilityJobItem))
        item.state = ItemState.IN_PROGRESS
        file = await session.scalar(select(LibraryFile))
        path = Path(file.file_path)
        before = path.read_bytes(), path.stat().st_mtime_ns
    if tamper:
        with ZipFile(path, "a") as archive:
            archive.writestr("new-page.jpg", b"externally changed")
        before = path.read_bytes(), path.stat().st_mtime_ns
    await recover_file_metadata_jobs(factory)
    async with factory() as session:
        assert (await session.get(PendingMatch, pending_id)).status == (
            "pending" if tamper else "approved"
        )
        assert (await session.get(DirectAcquisitionAttempt, attempt_id)).state == (
            DirectAcquisitionState.INTERVENTION if tamper else DirectAcquisitionState.COMPLETED
        )
        assert (await session.get(UtilityJob, job_id)).state == (
            JobState.FAILED if tamper else JobState.COMPLETED
        )
        assert (await session.scalar(select(DownloadHistory))).state == (
            DownloadState.FAILED if tamper else DownloadState.IMPORTED
        )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.usefixtures("paired_download_setting")
async def test_verified_replacement_retires_obsolete_recovery_without_claiming_transfer_succeeded(
    identity_probe_db, tmp_path
):
    from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata

    _, factory, _ = identity_probe_db
    pending_id, attempt_id, issue_id, source = await _review_case(factory, tmp_path)
    choices = {"series.issue_count": "library"}
    async with factory() as session:
        preview = await prepare_file_metadata(session, issue_id, choices=choices)
    await write_file_metadata(
        factory,
        issue_id,
        preview.preview.review_key,
        uuid4(),
        choices=choices,
        limit=10 * 1024**2,
        check_control=AsyncMock(),
        progress=AsyncMock(),
    )
    async with factory() as session:
        replacement = await session.scalar(select(LibraryFile))
        path = Path(replacement.file_path)
        before = path.read_bytes(), path.stat().st_mtime_ns
        recovery = await prepare_direct_metadata_recovery(session, pending_id)
    assert recovery.preview.superseded and recovery.preview.unchanged
    await run_direct_metadata_recovery(
        factory,
        pending_id,
        recovery.preview.review_key,
        uuid4(),
        choices={},
        limit=10 * 1024**2,
        check_control=AsyncMock(),
        progress=AsyncMock(),
    )
    async with factory() as session:
        assert (await session.get(PendingMatch, pending_id)).match_details[
            "resolution_reason"
        ] == "metadata_superseded"
        assert (
            await session.get(DirectAcquisitionAttempt, attempt_id)
        ).state == DirectAcquisitionState.FAILED
        assert (await session.scalar(select(DownloadHistory))).state == DownloadState.FAILED
        assert (await session.get(Issue, issue_id)).status == IssueStatus.OWNED
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert source.is_file()


@pytest.fixture(autouse=True)
def paired_restore_enabled(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
