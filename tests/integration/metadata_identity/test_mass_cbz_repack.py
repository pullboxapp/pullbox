"""Same-path Mass conversion retains its normal ownership and recovery contract."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.models import Issue, LibraryFile, LibraryRoot
from pullbox.models.library import FileFormat, LibraryFileStorageMode
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services import library_conversion_files as files
from pullbox.services import library_conversion_recovery as recovery
from pullbox.services import library_convert_service
from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors import mass_paired_conversion
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from pullbox.utilities.executors.rollback_executor import RollbackExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem
from tests.integration.metadata_identity.test_library_paired_conversion import (
    paired_conversion_setting,  # noqa: F401
)
from tests.integration.metadata_identity.test_mass_paired_conversion import execute, prepare_mass

pytestmark = pytest.mark.usefixtures("paired_conversion_setting")


async def test_same_path_runs_through_real_queue_and_rollback(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    _, config, _, _, source, file_id, old_id, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    original = source.read_bytes()
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.MASS_CONVERT_PIPELINE, MassConvertPipelineExecutor)
    manager.register_executor(JobType.ROLLBACK, RollbackExecutor)
    async with factory.begin() as session:
        await session.delete(await session.get(UtilityJob, old_id))
        job = await manager.create_job(session, JobType.MASS_CONVERT_PIPELINE, "CBZ pair", config)
        job_id = job.id
    await manager.dispatch_next()
    async with factory.begin() as session:
        job = await session.get(UtilityJob, job_id)
        assert job.state == JobState.COMPLETED, job.error_message
        assert job.completed_items == 1 and job.failed_items == 0
        assert source.read_bytes() != original
        await manager.queue_rollback_job(session, job_id)
    await manager.dispatch_next()
    assert source.read_bytes() == original
    async with factory() as session:
        assert (await session.get(UtilityJob, job_id)).state == JobState.ROLLED_BACK
        file = await session.get(LibraryFile, file_id)
        assert file.file_path == str(source) and file.file_format is FileFormat.CBZ


@pytest.mark.parametrize("point", ["intent", "backup", "output", "changed_output"])
async def test_same_path_recovers_only_proven_publication(
    identity_probe_db, tmp_path, monkeypatch, point
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, job_id, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    original = source.read_bytes()

    def crash(plan):
        if point != "intent":
            files.publish_file_without_overwrite(plan.backup_stage, plan.backup.path)
        if point in {"output", "changed_output"}:
            os.replace(plan.output_stage, plan.output.path)
        if point == "changed_output":
            source.write_bytes(b"user changed the output")
        raise OSError("simulated process interruption")

    monkeypatch.setattr(recovery, "publish", crash)
    result = await execute(executor, config, context, item)
    assert result.result is (ItemResult.COMPLETED if point == "output" else ItemResult.FAILED)
    async with factory() as session:
        journal = await session.scalar(select(LibraryConversion))
        expected = (
            "complete"
            if point == "output"
            else "review"
            if point == "changed_output"
            else "abandoned"
        )
        assert journal.state == expected
        assert await recovery.recover_conversion(session, UUID(item["id"])) == expected
    if point == "output":
        before = source.read_bytes(), source.stat()
        second = await execute(executor, config, context, item)
        assert second.result is ItemResult.COMPLETED
        assert second.after_state == result.after_state
        assert (source.read_bytes(), source.stat()) == before
        async with factory.begin() as session:
            (await session.get(UtilityJob, job_id)).state = JobState.CANCELLING
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.MASS_CONVERT_PIPELINE, MassConvertPipelineExecutor)
        await manager.recover_and_dispatch()
        async with factory() as session:
            saved = await session.get(UtilityJobItem, item["id"])
            assert saved.state == ItemState.COMPLETED
            assert (
                json.loads(saved.after_state)["conversion_plan"]
                == result.after_state["conversion_plan"]
            )
            assert (await session.get(UtilityJob, job_id)).state == JobState.CANCELLED
    else:
        assert source.read_bytes() == (
            b"user changed the output" if point == "changed_output" else original
        )


@pytest.mark.parametrize("changed", ["path", "original_path"])
async def test_same_path_rollback_refuses_changed_files(identity_probe_db, tmp_path, changed):
    _, factory, _ = identity_probe_db
    executor, config, context, item, _, _, _, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, result.error_message
    Path(result.after_state[changed]).write_bytes(b"new user bytes")
    before = {key: Path(result.after_state[key]).read_bytes() for key in ("path", "original_path")}
    restored = executor.rollback_item(
        {"id": item["id"], "before_state": result.before_state, "after_state": result.after_state},
        config,
    )
    assert restored.result is ItemResult.FAILED
    assert {key: Path(result.after_state[key]).read_bytes() for key in before} == before


@pytest.mark.parametrize("condition", ["reference", "readonly_root", "readonly_file", "conflict"])
async def test_same_path_protected_sources_remain_untouched(identity_probe_db, tmp_path, condition):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, file_id, _, root_id = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        if condition == "reference":
            file.storage_mode = LibraryFileStorageMode.REFERENCED
        elif condition == "readonly_root":
            (await session.get(LibraryRoot, root_id)).allow_managed_writes = False
        elif condition == "conflict":
            (await session.get(Issue, file.issue_id)).issue_number_text = "50-o"
    if condition == "readonly_file":
        source.chmod(0o444)
    before = source.read_bytes(), source.stat()
    try:
        result = await execute(executor, config, context, item)
        assert result.result is (
            ItemResult.FAILED if condition == "conflict" else ItemResult.SKIPPED
        )
        assert (source.read_bytes(), source.stat()) == before
        assert not list(tmp_path.rglob(".pullbox-conversion-*"))
    finally:
        source.chmod(0o644)


async def test_same_path_cancel_drains_preparation(identity_probe_db, tmp_path, monkeypatch):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, job_id, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    entered, exited = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            exited.set()

    monkeypatch.setattr(library_convert_service, "prepare_conversion", pending)
    original = source.read_bytes(), source.stat()
    task = asyncio.create_task(execute(executor, config, context, item))
    await asyncio.wait_for(entered.wait(), 3)
    async with factory.begin() as session:
        (await session.get(UtilityJob, job_id)).state = JobState.CANCELLING
    result = await asyncio.wait_for(task, 3)
    assert result.result is ItemResult.CANCELLED and exited.is_set()
    assert (source.read_bytes(), source.stat()) == original


async def test_same_path_lost_acknowledgement_keeps_rollback(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, _, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    original = source.read_bytes()
    convert = mass_paired_conversion.convert_library_file

    async def lose_ack(*args, **kwargs):
        await convert(*args, **kwargs)
        raise OSError("lost success acknowledgement")

    monkeypatch.setattr(mass_paired_conversion, "convert_library_file", lose_ack)
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.COMPLETED, result.error_message
    assert Path(result.after_state["original_path"]).read_bytes() == original
    assert source.exists()


@pytest.mark.parametrize("enabled,repack", [(False, True), (True, False)])
async def test_same_path_requires_explicit_paired_admission(
    identity_probe_db, tmp_path, monkeypatch, enabled, repack
):
    _, factory, _ = identity_probe_db
    _, _, _, _, source, _, _, _ = await prepare_mass(factory, tmp_path, FileFormat.CBZ)
    original = source.read_bytes(), source.stat()
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", str(enabled))
    get_settings.cache_clear()
    async with factory() as session:
        with pytest.raises(ValidationError, match="already exists"):
            await library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
                repack_cbz=repack,
            )
    assert (source.read_bytes(), source.stat()) == original


async def test_same_path_detects_source_change_after_backup(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    executor, config, context, item, source, _, _, _ = await prepare_mass(
        factory, tmp_path, FileFormat.CBZ
    )
    original = source.read_bytes()
    publish = files.publish_file_without_overwrite

    def change_source(stage, destination):
        publish(stage, destination)
        if destination.parent.name.startswith("conversion-"):
            source.write_bytes(b"new user bytes after backup")

    monkeypatch.setattr(files, "publish_file_without_overwrite", change_source)
    result = await execute(executor, config, context, item)
    assert result.result is ItemResult.FAILED
    assert source.read_bytes() == b"new user bytes after backup"
    async with factory() as session:
        row = await session.scalar(select(LibraryConversion))
        assert row.state == "review" and row.active
        assert files.decode_plan(row.plan_json).backup.path.read_bytes() == original
