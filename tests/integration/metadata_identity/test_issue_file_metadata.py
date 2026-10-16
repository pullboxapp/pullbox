"""User-approved existing CBZ writes preserve pages, edits and durable results."""

from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select, update

from pullbox.core.exceptions import JobCancelledError
from pullbox.models import Issue, LibraryFile
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.archive_metadata_publication import (
    PublicationState,
    publish_archive_publication,
)
from pullbox.services.issue_file_metadata import (
    prepare_file_metadata,
    recover_file_metadata,
    write_file_metadata,
)
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared, record


async def noop(*args):
    pass


async def preview(factory, issue_id):
    async with factory() as session:
        return await prepare_file_metadata(session, issue_id)


async def test_approved_job_writes_both_documents_and_preserves_pages_and_manual_values(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    xml = (
        "<ComicInfo><Number>50-X</Number><Summary>My embedded note</Summary>"
        "<Writer>My Writer</Writer></ComicInfo>"
    )
    async with prepared(factory, tmp_path, comicinfo=xml) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        before = path.read_bytes()
        reviewed = await preview(factory, issue_id)
        assert path.read_bytes() == before
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
        async with factory.begin() as session:
            job = await manager.create_job(
                session,
                JobType.FILE_METADATA,
                "Write test metadata",
                {"issue_id": issue_id, "review_key": reviewed.preview.review_key},
            )
            job_id = job.id
        await manager.dispatch_next()
        async with factory() as session:
            job = await session.get(UtilityJob, job_id)
            assert job.state == JobState.COMPLETED, job.error_message
            assert job.completed_items == 1 and job.failed_items == 0
            item = await session.scalar(select(UtilityJobItem))
            assert item.state == ItemState.COMPLETED, item.error_message
            receipt = await session.scalar(select(ArchiveMetadataPublication))
            assert receipt.state is PublicationState.FINALIZED
            assert receipt.active_file_id is None
            issue = await session.get(Issue, issue_id)
            assert issue.description == "My embedded note"
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            assert file.file_size == path.stat().st_size and file.has_comicinfo
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive.namelist())
            assert b"My embedded note" in archive.read("ComicInfo.xml")
            assert b"My embedded note" in archive.read("MetronInfo.xml")
        second = await preview(factory, issue_id)
        assert second.preview.unchanged
        stat = path.stat()
        outcome = await write_file_metadata(
            factory,
            issue_id,
            second.preview.review_key,
            uuid4(),
            limit=1000000,
            check_control=noop,
            progress=noop,
        )
        assert outcome == "unchanged" and path.stat() == stat


@pytest.mark.parametrize("change", ["source", "metadata", "cancel"])
async def test_changed_approval_and_cancel_never_replace_original(
    identity_probe_db, tmp_path, change
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        reviewed = await preview(factory, issue_id)
        if change == "metadata":
            async with factory.begin() as session:
                await session.execute(update(Issue).values(title="New manual title"))
        elif change == "source":
            path.touch()
        original = path.read_bytes(), path.stat()

        async def control():
            if change == "cancel":
                raise JobCancelledError("Stop")

        with pytest.raises((ValueError, JobCancelledError)):
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=control,
                progress=noop,
            )
        actual = path.read_bytes(), path.stat()
        assert actual[0] == original[0]
        # Reading may update atime; identity, ownership, mode and size must not change.
        assert actual[1][:7] == original[1][:7]
        assert actual[1].st_mtime_ns == original[1].st_mtime_ns
        assert actual[1].st_ctime_ns == original[1].st_ctime_ns


async def test_restart_recovers_published_result_without_repacking(identity_probe_db, tmp_path):
    from tests.integration.metadata_identity.test_archive_metadata_finalization import published

    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        receipt, _ = await published(factory, plan)
        before = path.read_bytes(), path.stat()
        assert (
            await recover_file_metadata(factory, receipt.operation_id) is PublicationState.FINALIZED
        )
        assert (
            await recover_file_metadata(factory, receipt.operation_id) is PublicationState.FINALIZED
        )
        assert (path.read_bytes(), path.stat()) == before


async def test_cancel_after_intent_abandons_only_untouched_original(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        reviewed = await preview(factory, issue_id)
        original = path.read_bytes(), path.stat()

        async def stop_after_intent():
            async with factory() as session:
                if await session.scalar(select(ArchiveMetadataPublication)):
                    raise JobCancelledError("Stop after intent")

        with pytest.raises(JobCancelledError):
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=stop_after_intent,
                progress=noop,
            )
        assert (path.read_bytes(), path.stat()) == original
        async with factory() as session:
            receipt = await session.scalar(select(ArchiveMetadataPublication))
            assert receipt.state is PublicationState.ABANDONED
            assert receipt.active_file_id is None


async def test_cancel_during_archive_preparation_preserves_original(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        reviewed = await preview(factory, issue_id)
        original = path.read_bytes(), path.stat()

        async def stop_on_progress(*args):
            raise JobCancelledError("Stop staging")

        with pytest.raises(JobCancelledError):
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=noop,
                progress=stop_on_progress,
            )
        assert (path.read_bytes(), path.stat()) == original


async def test_import_rollback_protection_never_changes_the_file(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            job = ImportJob(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
            )
            session.add(job)
            await session.flush()
            series = ImportedSeries(import_job_id=job.id, raw_series_name="Example")
            session.add(series)
            await session.flush()
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path=str(path),
                    file_name=path.name,
                    file_format="cbz",
                    library_file_id=plan.target.binding.library_file_id,
                )
            )
        original = path.read_bytes(), path.stat()
        with pytest.raises(ArchiveMetadataBindingError, match="import_rollback_protected"):
            await preview(factory, issue_id)
        assert (path.read_bytes(), path.stat()) == original


@pytest.mark.parametrize("state", [JobState.RUNNING, JobState.CANCELLING])
async def test_utility_restart_settles_published_metadata_even_after_cancel(
    identity_probe_db, tmp_path, state
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        job_id = uuid4().hex
        operation = uuid4()
        async with factory.begin() as session:
            session.add(
                UtilityJob(
                    id=job_id,
                    job_type=JobType.FILE_METADATA,
                    display_name="Interrupted metadata",
                    state=state,
                    total_items=1,
                    config="{}",
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
        plan = plan.model_copy(update={"metadata_job_id": job_id})
        receipt = await record(factory, plan, operation=operation)
        async with factory.begin() as session:
            await publish_archive_publication(session, receipt.operation_id)
        before = path.read_bytes(), path.stat()
        manager = JobQueueManager(factory)
        await manager.recover_and_dispatch()
        async with factory() as session:
            row = await session.scalar(select(ArchiveMetadataPublication))
            assert row.state is PublicationState.FINALIZED
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            assert file.file_size == path.stat().st_size
            item = await session.get(UtilityJobItem, operation.hex)
            assert item.state == ItemState.COMPLETED
            job = await session.get(UtilityJob, job_id)
            assert job.completed_items == 1
            assert job.state == (
                JobState.CANCELLED if state is JobState.CANCELLING else JobState.COMPLETED
            )
        assert (path.read_bytes(), path.stat()) == before


@pytest.mark.parametrize(
    "choice,value", [("library", "Library summary"), ("ComicInfo.xml", "File summary")]
)
async def test_explicit_choice_writes_one_snapshot_on_both_databases(
    identity_probe_db, tmp_path, choice, value
):
    from xml.etree import ElementTree as ET

    _, factory, _ = identity_probe_db
    xml = (
        "<ComicInfo><Number>50-X</Number><Summary>File summary</Summary>"
        "<Notes>Personal note</Notes></ComicInfo>"
    )
    async with prepared(factory, tmp_path, comicinfo=xml) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            await session.execute(
                update(Issue).where(Issue.id == issue_id).values(description="Library summary")
            )
        choices = {"issue.description": choice}
        async with factory() as session:
            pending = await prepare_file_metadata(session, issue_id)
            assert not pending.preview.ready
            reviewed = await prepare_file_metadata(session, issue_id, choices=choices)
            assert reviewed.preview.ready
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
        async with factory.begin() as session:
            job = await manager.create_job(
                session,
                JobType.FILE_METADATA,
                "Chosen metadata",
                {
                    "issue_id": issue_id,
                    "review_key": reviewed.preview.review_key,
                    "choices": choices,
                },
            )
            job_id = job.id
        await manager.dispatch_next()
        async with factory() as session:
            job = await session.get(UtilityJob, job_id)
            assert job.state == JobState.COMPLETED, job.error_message
            assert (await session.get(Issue, issue_id)).description == value
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            for name in ("ComicInfo.xml", "MetronInfo.xml"):
                assert ET.fromstring(archive.read(name)).findtext("Summary") == value
            assert "Personal note" in ET.fromstring(archive.read("ComicInfo.xml")).findtext("Notes")
        assert (await preview(factory, issue_id)).preview.unchanged


@pytest.mark.parametrize("change", ["choices", "source", "metadata", "cancel"])
async def test_conflict_approval_is_rechecked_before_staging(identity_probe_db, tmp_path, change):
    _, factory, _ = identity_probe_db
    async with prepared(
        factory,
        tmp_path,
        comicinfo="<ComicInfo><Number>50-X</Number><Summary>File summary</Summary></ComicInfo>",
    ) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            await session.execute(update(Issue).values(description="Library summary"))
        choices = {"issue.description": "library"}
        async with factory() as session:
            reviewed = await prepare_file_metadata(session, issue_id, choices=choices)
        if change == "choices":
            choices["issue.description"] = "ComicInfo.xml"
        elif change == "source":
            path.touch()
        elif change == "metadata":
            async with factory.begin() as session:
                await session.execute(update(Issue).values(description="New library summary"))
        original = path.read_bytes(), path.stat()

        async def control():
            if change == "cancel":
                raise JobCancelledError("Stop")

        with pytest.raises((ValueError, JobCancelledError)):
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=control,
                progress=noop,
                choices=choices,
            )
        assert (path.read_bytes(), path.stat()) == original
