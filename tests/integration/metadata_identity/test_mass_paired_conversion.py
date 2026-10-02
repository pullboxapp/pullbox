"""The actual Mass Convert lane shares paired conversion and exact rollback evidence."""

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import select

from pullbox.models import Issue, LibraryFile, LibraryRoot
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import FileFormat, LibraryFileStorageMode
from pullbox.models.library_conversion import LibraryConversion
from pullbox.utilities.base_executor import ExecutionMode, ItemResult, JobRunSummary
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from pullbox.utilities.executors.rollback_executor import RollbackExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem
from tests.integration.metadata_identity.test_library_paired_conversion import (
    paired_conversion_setting,  # noqa: F401
    registered_nonzip,
)
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source


async def prepare_mass(factory, tmp_path, source_format=FileFormat.CB7, scope="manual"):
    source, file_id, _, _, root_id = await registered_nonzip(
        factory, tmp_path, FileFormat.CB7 if source_format is FileFormat.PDF else source_format
    )
    if source_format is FileFormat.PDF:
        source.unlink()
        source = pdf_source(source.with_suffix(".pdf"))
    job_id, item_id = uuid4().hex, uuid4().hex
    config = {
        "scope": scope,
        "file_paths": [str(source)],
        "scan_folder": str(source.parent),
        "steps": [1, 2, 4],
        "trash_folder": str(tmp_path / "trash"),
    }
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.file_path, file.file_name, file.file_format = str(source), source.name, source_format
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
        if source_format is FileFormat.PDF:
            (await session.get(Issue, file.issue_id)).page_count = 3
        session.add(
            UtilityJob(
                id=job_id,
                job_type=JobType.MASS_CONVERT_PIPELINE,
                display_name="Paired conversion",
                state=JobState.RUNNING,
                total_items=1,
                config=json.dumps(config),
            )
        )
        await session.flush()
        session.add(
            UtilityJobItem(
                id=item_id,
                job_id=job_id,
                state=ItemState.IN_PROGRESS,
                item_index=0,
                operation="pipeline",
                file_path=str(source),
            )
        )
    async with factory() as session:
        executor = MassConvertPipelineExecutor(session)
        context = await executor.build_job_context(session, config)
    item = next(item for item in context["items"] if item.get("library_file_id") == file_id)
    item["id"] = item_id
    return executor, config, context, item, source, file_id, job_id, root_id


async def execute(executor, config, context, item):
    if executor.get_execution_mode(config, context) is ExecutionMode.ASYNC:
        return await executor.process_item_async(item, config, context)
    return executor.process_item(item, config, context)


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("scope", ["manual", "folder", "library"])
@pytest.mark.parametrize(
    "source_format",
    [
        FileFormat.CB7,
        pytest.param(
            FileFormat.CBR,
            marks=pytest.mark.skipif(
                not shutil.which("unrar"), reason="Native UnRAR required in Docker"
            ),
        ),
        pytest.param(FileFormat.PDF, marks=native_pdf),
    ],
)
async def test_mass_lane_publishes_pair_and_rolls_back_exact_source(
    identity_probe_db, tmp_path, scope, source_format
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, file_id, _, _ = await prepare_mass(
        factory, tmp_path, source_format, scope
    )
    original = source.read_bytes()
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, result.error_message
    output = Path(result.after_state["path"])
    with ZipFile(output) as archive:
        assert "MetronInfo.xml" in archive.namelist(), "Mass Convert still writes ComicInfo only"
        ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        if source_format is not FileFormat.PDF:
            assert archive.read("page.jpg") == b"page bytes"
        else:
            assert ci.findtext("PageCount") == mi.findtext("PageCount") == "3"
    assert executor.get_execution_mode(config, context) is ExecutionMode.ASYNC
    async with factory.begin() as session:
        await executor.apply_item_result(
            session, None, item, result, config, context, JobRunSummary()
        )
    rollback = {
        "id": item["id"],
        "file_path": str(output),
        "before_state": result.before_state,
        "after_state": result.after_state,
    }
    restored = executor.rollback_item(rollback, config)
    assert restored.result is ItemResult.COMPLETED, restored.error_message
    async with factory.begin() as session:
        await executor.apply_rollback_result(session, rollback, restored)
    assert source.read_bytes() == original and not output.exists()
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_format is source_format and file.file_path == str(source)


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("exclusion", ["reference", "read_only", "unregistered", "cbz"])
async def test_paired_mass_exclusions_leave_bytes_untouched(identity_probe_db, tmp_path, exclusion):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, file_id, _, root_id = await prepare_mass(
        factory, tmp_path
    )
    async with factory.begin() as session:
        if exclusion == "reference":
            (
                await session.get(LibraryFile, file_id)
            ).storage_mode = LibraryFileStorageMode.REFERENCED
        elif exclusion == "read_only":
            (await session.get(LibraryRoot, root_id)).allow_managed_writes = False
        elif exclusion == "unregistered":
            await session.delete(await session.get(LibraryFile, file_id))
        else:
            # The same-path lane must not repack an existing CBZ.
            item["file_path"] = str(source.with_suffix(".cbz"))
            source.rename(item["file_path"])
            source = Path(item["file_path"])
    before = source.read_bytes(), source.stat()
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.SKIPPED, result.error_message
    assert (source.read_bytes(), source.stat()) == before
    assert result.warning_message


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_completed_mass_receipt_is_reused_without_conversion(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    executor, config, context, item, _source, _, _, _ = await prepare_mass(factory, tmp_path)
    first = await execute(executor, config, context, item)
    assert first.result is ItemResult.COMPLETED, first.error_message
    output = Path(first.after_state["path"])
    before = output.read_bytes(), output.stat()
    second = await executor.process_item_async(item, config, context)
    assert second.result is ItemResult.COMPLETED, second.error_message
    assert second.after_state == first.after_state
    assert (output.read_bytes(), output.stat()) == before
    async with factory() as session:
        assert len((await session.scalars(select(LibraryConversion))).all()) == 1


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("changed", ["path", "original_path"])
async def test_mass_rollback_refuses_changed_public_artifact(identity_probe_db, tmp_path, changed):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, _, _ = await prepare_mass(factory, tmp_path)
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, result.error_message
    Path(result.after_state[changed]).write_bytes(b"changed by another application")
    before = {key: Path(result.after_state[key]).read_bytes() for key in ("path", "original_path")}
    rollback = {
        "id": item["id"],
        "before_state": result.before_state,
        "after_state": result.after_state,
    }
    restored = executor.rollback_item(rollback, config)
    assert restored.result is ItemResult.FAILED, "Rollback deleted modified library bytes"
    assert not source.exists()
    assert {key: Path(result.after_state[key]).read_bytes() for key in before} == before


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_committed_cancel_interrupts_mass_preparation(
    identity_probe_db, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from pullbox.services import library_convert_service

    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, job_id, _ = await prepare_mass(factory, tmp_path)
    entered = asyncio.Event()

    @asynccontextmanager
    async def slow_prepare(*args, **kwargs):
        entered.set()
        await asyncio.sleep(60)
        yield

    monkeypatch.setattr(library_convert_service, "prepare_conversion", slow_prepare)
    before = source.read_bytes(), source.stat()
    task = asyncio.create_task(executor.process_item_async(item, config, context))
    await asyncio.wait_for(entered.wait(), 2)
    async with factory.begin() as session:
        (await session.get(UtilityJob, job_id)).state = JobState.CANCELLING
    result = await asyncio.wait_for(task, 2)
    assert result.result is ItemResult.CANCELLED
    assert (source.read_bytes(), source.stat()) == before
    assert not source.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_actual_mass_queue_and_rollback_keep_normal_job_counters(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    _, config, _, _, source, file_id, old_job_id, _ = await prepare_mass(factory, tmp_path)
    original = source.read_bytes()
    async with factory.begin() as session:
        first = await session.get(LibraryFile, file_id)
        second_path = source.with_name("second-book.cb7")
        second_path.write_bytes(original)
        session.add(
            LibraryFile(
                file_path=str(second_path),
                file_name=second_path.name,
                file_format=FileFormat.CB7,
                file_size=second_path.stat().st_size,
                file_modified_at=datetime.fromtimestamp(second_path.stat().st_mtime, UTC),
                library_root_id=first.library_root_id,
                issue_id=first.issue_id,
            )
        )
        config["file_paths"].append(str(second_path))
        await session.delete(await session.get(UtilityJob, old_job_id))
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.MASS_CONVERT_PIPELINE, MassConvertPipelineExecutor)
        manager.register_executor(JobType.ROLLBACK, RollbackExecutor)
        job = await manager.create_job(
            session, JobType.MASS_CONVERT_PIPELINE, "Actual paired queue", config
        )
        job_id = job.id
    await manager.dispatch_next()
    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.COMPLETED, job.error_message
        assert job.completed_items == 2 and job.failed_items == 0
        for item in (
            await session.scalars(select(UtilityJobItem).where(UtilityJobItem.job_id == job_id))
        ).all():
            assert item.state == ItemState.COMPLETED
            after = json.loads(item.after_state)
            with ZipFile(after["path"]) as archive:
                assert "MetronInfo.xml" in archive.namelist()
    async with factory.begin() as session:
        await manager.queue_rollback_job(session, job_id)
    await manager.dispatch_next()
    assert source.read_bytes() == original
    assert second_path.read_bytes() == original
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_format is FileFormat.CB7
        assert (await session.get(UtilityJob, job_id)).state == JobState.ROLLED_BACK


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("state", [JobState.RUNNING, JobState.CANCELLING])
async def test_mass_restart_recovers_receipt_into_utility_rollback_journal(
    identity_probe_db, tmp_path, state
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, job_id, _ = await prepare_mass(factory, tmp_path)
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, result.error_message
    # Model the exact crash window: Library conversion committed, utility result not saved.
    async with factory.begin() as session:
        (await session.get(UtilityJob, job_id)).state = state
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.MASS_CONVERT_PIPELINE, MassConvertPipelineExecutor)
    await manager.recover_and_dispatch()
    async with factory() as session:
        saved = await session.get(UtilityJobItem, item["id"])
        assert saved.state == ItemState.COMPLETED, (
            "A committed conversion lost its Utilities rollback receipt"
        )
        assert (
            json.loads(saved.after_state)["conversion_plan"]
            == result.after_state["conversion_plan"]
        )
        job = await session.get(UtilityJob, job_id)
        assert job.completed_items == 1
        assert job.state == (
            JobState.CANCELLED if state is JobState.CANCELLING else JobState.COMPLETED
        )
    assert not source.exists()


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_import_owned_mass_file_is_skipped_without_detaching_ownership(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, file_id, _, _ = await prepare_mass(factory, tmp_path)
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        issue = await session.get(Issue, file.issue_id)
        job = ImportJob(
            source_path="retained import",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
        )
        session.add(job)
        await session.flush()
        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Canonical series",
            series_id=issue.series_id,
            status=ImportSeriesStatus.IMPORTED,
        )
        session.add(series)
        await session.flush()
        owner = ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path=str(source),
            file_name=source.name,
            file_format="cb7",
            file_size=source.stat().st_size,
            library_file_id=file_id,
            matched_issue_id=issue.id,
            status=ImportedFileStatus.IMPORTED,
        )
        session.add(owner)
        await session.flush()
        owner_id = owner.id
    before = source.read_bytes(), source.stat()
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.SKIPPED and "rollback" in result.warning_message
    assert (source.read_bytes(), source.stat()) == before
    async with factory() as session:
        assert (await session.get(ImportedFile, owner_id)).library_file_id == file_id


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_mass_metadata_conflict_remains_actionable_without_publication(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, file_id, _, _ = await prepare_mass(factory, tmp_path)
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        (await session.get(Issue, file.issue_id)).issue_number_text = "50-o"
    before = source.read_bytes(), source.stat()
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.FAILED
    assert "metadata" in result.error_message and "Review" in result.error_message
    assert (source.read_bytes(), source.stat()) == before
    assert not source.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_mass_lost_success_acknowledgement_keeps_rollback_receipt(
    identity_probe_db, tmp_path, monkeypatch
):
    from pullbox.utilities.executors import mass_paired_conversion

    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, _, _ = await prepare_mass(factory, tmp_path)
    original_convert = mass_paired_conversion.convert_library_file

    async def lose_acknowledgement(*args, **kwargs):
        await original_convert(*args, **kwargs)
        raise OSError("success acknowledgement lost")

    monkeypatch.setattr(mass_paired_conversion, "convert_library_file", lose_acknowledgement)
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, (
        "Durable successful conversion lost its utility rollback receipt"
    )
    assert result.after_state["conversion_plan"]
    assert not source.exists()


@pytest.mark.parametrize("enabled,steps", [(False, [1, 2, 4]), (True, [1, 4])])
async def test_mass_legacy_context_stays_picklable_when_paired_step_is_off(
    identity_probe_db, tmp_path, monkeypatch, enabled, steps
):
    import pickle

    from pullbox.config import get_settings

    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", str(enabled))
    get_settings.cache_clear()
    try:
        _, factory, _ = identity_probe_db
        _, config, _, _, _, _, _, _ = await prepare_mass(factory, tmp_path)
        config["steps"] = steps
        async with factory() as session:
            executor = MassConvertPipelineExecutor(session)
            context = await executor.build_job_context(session, config)
        assert executor.get_execution_mode(config, context) is ExecutionMode.PROCESS
        assert "factory" not in context
        assert pickle.loads(pickle.dumps(context)) == context
    finally:
        get_settings.cache_clear()
