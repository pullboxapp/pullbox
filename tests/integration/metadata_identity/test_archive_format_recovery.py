"""Embedded formats are not database enums; publication recovery never repacks."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models import LibraryFile, Series
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication, PublicationState
from pullbox.models.pending_match import PendingMatch
from pullbox.models.series import SeriesType
from pullbox.services.direct_metadata_recovery import prepare_direct_metadata_recovery
from pullbox.services.issue_file_metadata import prepare_file_metadata
from pullbox.services.issue_file_metadata_recovery import recover_file_metadata_jobs
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobState, JobType, UtilityJob, UtilityJobItem
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    direct_artifact,
    execute,
    no_global_sync,  # noqa: F401
    paired_download_setting,  # noqa: F401
)
from tests.unit.test_nonzip_metadata_writing import nonzip_archive


async def format_case(factory, tmp_path, label, *, suffix=".cbz"):
    attempt, artifact, issue_id, source = await direct_artifact(
        factory, tmp_path, conflict=True, suffix=suffix
    )
    with ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"page bytes")
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Number>50-X</Number><Count>4</Count><Format>{label}</Format>"
            "<AgeRating>Teen</AgeRating></ComicInfo>",
        )
        archive.writestr(
            "MetronInfo.xml",
            f"<MetronInfo><Series><Name>Canonical series</Name><Format>{label}</Format>"
            "</Series><Number>50-X</Number><AgeRating>Unknown</AgeRating></MetronInfo>",
        )
    if suffix != ".cbz":
        with ZipFile(source) as archive:
            members = [(name, archive.read(name)) for name in archive.namelist()]
        source.unlink()
        nonzip_archive(source, members)
    await execute(factory, tmp_path, attempt, artifact)
    choices = {"series.series_type": "ComicInfo.xml", "series.issue_count": "library"}
    async with factory() as session:
        pending_id = (await session.scalar(select(PendingMatch))).id
        reviewed = await prepare_direct_metadata_recovery(session, pending_id, choices=choices)
        assert reviewed.preview.ready
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
    async with factory.begin() as session:
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            "Review embedded format",
            {
                "issue_id": issue_id,
                "recovery_pending_id": pending_id,
                "review_key": reviewed.preview.review_key,
                "choices": choices,
            },
        )
        job_id = job.id
    return manager, job_id, pending_id, issue_id, source


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("suffix", [".cbz", ".cb7"])
@pytest.mark.parametrize(
    "label,classification",
    [
        ("Limited Series", SeriesType.STANDARD),
        ("Digital Chapter", SeriesType.STANDARD),
        ("Hardcover", SeriesType.HARDCOVER),
    ],
)
async def test_selected_archive_format_completes_and_preview_is_stable(
    identity_probe_db, tmp_path, label, classification, suffix
):
    _, factory, _ = identity_probe_db
    manager, job_id, pending_id, issue_id, source = await format_case(
        factory, tmp_path, label, suffix=suffix
    )
    original = source.read_bytes()
    await manager.dispatch_next()
    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        item = await session.scalar(select(UtilityJobItem).where(UtilityJobItem.job_id == job_id))
        assert job.state == JobState.COMPLETED, item.error_message
        assert (await session.scalar(select(Series))).series_type is classification
        assert (await session.get(PendingMatch, pending_id)).status == "approved"
        file = await session.scalar(select(LibraryFile))
        with ZipFile(file.file_path) as archive:
            assert ET.fromstring(archive.read("ComicInfo.xml")).findtext("Format") == label
            assert ET.fromstring(archive.read("MetronInfo.xml")).findtext("Series/Format") == label
            assert archive.read("page.jpg") == b"page bytes"
        preview = (await prepare_file_metadata(session, issue_id)).preview
        assert preview.ready and preview.unchanged and not preview.conflicts
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("tamper", [False, True])
async def test_legacy_published_format_journal_finishes_without_archive_writes(
    identity_probe_db, tmp_path, monkeypatch, tamper
):
    from pullbox.services import issue_file_metadata as writer

    _, factory, _ = identity_probe_db
    manager, job_id, pending_id, _, source = await format_case(factory, tmp_path, "Limited Series")
    original = source.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(
            writer,
            "finalize_archive_publication",
            AsyncMock(side_effect=ValueError("lost acknowledgement")),
        )
        await manager.dispatch_next()
    async with factory.begin() as session:
        receipt = await session.scalar(select(ArchiveMetadataPublication))
        assert receipt.state is PublicationState.PUBLISHED
        # Recreate the pre-fix persisted snapshot, not a new write approved by new code.
        payload = json.loads(receipt.plan_json)
        payload["series"]["values"]["series_type"] = "Limited Series"
        payload["series"]["values"].pop("archive_format", None)
        payload["series"]["origins"] = [
            {**origin, "field": "series_type"} if origin["field"] == "archive_format" else origin
            for origin in payload["series"]["origins"]
            if origin["field"] != "series_type"
        ]
        receipt.plan_json = json.dumps(payload)
        path = Path((await session.scalar(select(LibraryFile))).file_path)
    if tamper:
        with ZipFile(path, "a") as archive:
            archive.writestr("unexpected.jpg", b"external change")
    before = path.read_bytes(), path.stat().st_mtime_ns
    with monkeypatch.context() as patch:
        patch.setattr(
            writer, "write_file_metadata", AsyncMock(side_effect=AssertionError("must not rewrite"))
        )
        patch.setattr(
            writer,
            "stage_cbz_metadata_interruptible",
            AsyncMock(side_effect=AssertionError("must not stage")),
        )
        await recover_file_metadata_jobs(factory)
        await recover_file_metadata_jobs(factory)
    async with factory() as session:
        assert (await session.get(UtilityJob, job_id)).state == (
            JobState.FAILED if tamper else JobState.COMPLETED
        )
        if not tamper:
            assert (await session.get(UtilityJob, job_id)).failed_items == 0
        assert (await session.get(PendingMatch, pending_id)).status == (
            "pending" if tamper else "approved"
        )
        assert (await session.scalar(select(Series))).series_type is SeriesType.STANDARD
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert source.read_bytes() == original


async def test_unpersistable_status_is_rejected_before_publication(identity_probe_db, tmp_path):
    from pullbox.services.archive_metadata_publication import prepare_archive_publication
    from tests.integration.metadata_identity.test_archive_metadata_publication import prepared

    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, staged, plan):
        before = path.read_bytes()
        staged = replace(staged, output_fingerprint=plan.stage_fingerprint)
        invalid = plan.series.model_copy(
            update={"values": plan.series.values.model_copy(update={"status": "invalid"})}
        )
        with pytest.raises(ValueError, match="invalid_database_metadata"):
            await prepare_archive_publication(plan.target, staged, invalid, plan.issue)
        assert path.read_bytes() == before
