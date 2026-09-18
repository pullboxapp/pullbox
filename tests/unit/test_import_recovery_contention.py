"""File-backed SQLite regressions for completed-import recovery."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_deferred_recovery_execution import save_recovery_state
from pullbox.services.import_file_execution import process_import_series_files
from pullbox.services.import_job_actions import record_action
from pullbox.services.import_referenced_sources import MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY
from pullbox.services.import_workflow_state import persist_progress_snapshot
from tests.unit.test_import_file_execution import (
    _mock_register_library_file,
    _setup_full_scenario,
)


@pytest.fixture
async def recovery_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _record):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=25")
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize(
    "failure",
    [None, "once", "actual_lock", "journal_once", "always", "cancel", "second", "not_locked"],
)
async def test_in_place_workers_serialize_retry_and_preserve_decisions(
    recovery_factory, monkeypatch, tmp_path, source_type, failure
):
    async with recovery_factory() as session:
        job, item, files, _, _ = await _setup_full_scenario(session, num_issues=2)
        job.source_type = source_type
        job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
        job.effective_transfer_method = "leave_in_place"
        job.update_embedded_comicinfo_from_match = False
        job.convert_to_preferred_format = False
        root = await session.get(LibraryRoot, job.target_library_root_id)
        root.path = str(Path(files[0].file_path).parent)
        for file in files:
            file.source_signature = {
                **file.source_signature,
                MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY: root.id,
            }
        paths = [Path(file.file_path) for file in files]
        originals = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        job_id, item_id = job.id, item.id
        await session.commit()

    active = maximum = attempts = inspections = 0
    progress = []
    register = _mock_register_library_file()

    async def refresh(session, _job, _item, file):
        nonlocal inspections
        inspections += 1
        # Revalidation refreshes metadata before progress, just like a remount.
        file.diagnostics = {**file.diagnostics, "source_rechecked": True}
        if failure == "actual_lock" and inspections == 1:
            async with recovery_factory() as blocker:
                await blocker.execute(update(ImportJob).values(error_message="other writer"))
                try:
                    await session.flush()
                finally:
                    await blocker.rollback()
        await session.flush()

    monkeypatch.setattr("pullbox.services.import_file_execution.refresh_recovery_source", refresh)

    async def prepare(_session, _job, file, **_kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return SimpleNamespace(
                registration_source=Path(file.file_path),
                original_source=Path(file.file_path),
                converted=False,
                skip_embedded_comicinfo=True,
            )
        finally:
            active -= 1

    async def register_with_contention(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure == "not_locked":
            raise OperationalError("INSERT", {}, Exception("disk I/O error"))
        if (
            failure == "always"
            or (failure in {"once", "cancel"} and attempts == 1)
            or (failure == "second" and attempts > 1)
        ):
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return await register(*args, **kwargs)

    async def check_control(*_args):
        if failure == "cancel" and attempts:
            raise JobCancelledError("Cancelled while retrying")

    async def journal(*args, **kwargs):
        action = await record_action(*args, **kwargs)
        if failure == "journal_once" and attempts == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return action

    async def report(**kwargs):
        progress.append(kwargs)
        if kwargs["stage"] == "preparing":
            assert kwargs.get("live_only"), "Never await a second writer inside a file transaction"
        if not kwargs.get("live_only"):
            async with recovery_factory() as progress_session:
                await progress_session.execute(
                    update(ImportJob).where(ImportJob.id == job_id).values(error_message=None)
                )
                await progress_session.commit()

    async def execute():
        async with recovery_factory() as session:
            job = await session.get(ImportJob, job_id)
            item = await session.get(ImportedSeries, item_id)
            result = await process_import_series_files(
                session,
                job,
                item,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                load_trash_dir=AsyncMock(return_value=tmp_path / "trash"),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=check_control,
                prepare_file=prepare,
                build_comicinfo_payload=AsyncMock(),
                apply_comicinfo=lambda *_args: None,
                cleanup_prepared_file=lambda *_args: None,
                record_action=journal,
                log_event=AsyncMock(),
                register_file=register_with_contention,
                move_to_trash=lambda *_args: None,
                report_file_progress=report,
                session_factory=recovery_factory,
                file_worker_count=4,
            )
            await session.commit()
            return result

    if failure in {"always", "cancel", "second"}:
        with pytest.raises(JobCancelledError if failure == "cancel" else JobPausedError):
            await execute()
    elif failure == "not_locked":
        assert await execute() == (0, 2)
        assert attempts == 2
    else:
        result = await execute()
        async with recovery_factory() as session:
            errors = list((await session.scalars(select(ImportedFile.error_message))).all())
        assert result == (2, 0), errors
        assert maximum == 1
        assert attempts == (3 if failure in {"once", "journal_once"} else 2)
        assert inspections == (3 if failure in {"once", "actual_lock", "journal_once"} else 2)
        assert any(
            p["stage"] == "finalizing" and p["current"] == p["total"] and not p.get("live_only")
            for p in progress
        )

    async with recovery_factory() as session:
        statuses = list(
            (await session.scalars(select(ImportedFile.status).order_by(ImportedFile.id))).all()
        )
        expected = (
            ImportedFileStatus.CONFIRMED
            if failure in {"always", "cancel"}
            else ImportedFileStatus.IMPORTED
        )
        if failure == "second":
            assert statuses == [ImportedFileStatus.IMPORTED, ImportedFileStatus.CONFIRMED]
        elif failure == "not_locked":
            assert statuses == [ImportedFileStatus.FAILED, ImportedFileStatus.FAILED]
        else:
            assert statuses == [expected, expected]
        expected_count = (
            0 if failure in {"always", "cancel", "not_locked"} else 1 if failure == "second" else 2
        )
        assert await session.scalar(select(func.count()).select_from(LibraryFile)) == (
            expected_count
        )
        actions = list((await session.scalars(select(ImportJobAction))).all())
        assert len(actions) == expected_count
        assert len({action.sequence_no for action in actions}) == len(actions)
    if failure == "second":
        failure = None
        assert await execute() == (1, 0)
        async with recovery_factory() as session:
            assert await session.scalar(select(func.count()).select_from(LibraryFile)) == 2
            assert await session.scalar(select(func.count()).select_from(ImportJobAction)) == 2
            assert set(await session.scalars(select(ImportedFile.status))) == {
                ImportedFileStatus.IMPORTED
            }
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == originals


@pytest.mark.parametrize("state", ["catalogs", "prepared", "completed"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_recovery_checkpoints_discard_only_consumed_catalog_caches(db_session, state, legacy):
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()
    scope = {"state": state, "run_id": "run", "series_ids": [1, 2], "actor_id": 3}
    caches = {
        key: {"huge": "x" * 200_000}
        for key in (
            "candidates",
            "completed",
            "matches",
            "title_candidates",
            "title_completed",
            "title_matches",
            "reference_candidates",
        )
    }
    original = {**scope, **caches, "catalog_files_prepared": 2}
    if legacy:
        job.progress_snapshot = {"deferred_recovery": original}
        await persist_progress_snapshot(
            db_session,
            job,
            ImportProgressEvent(job_id=job.id, status=job.status, phase="importing", progress=0),
        )
    else:
        save_recovery_state(job, original)
    saved = job.progress_snapshot["deferred_recovery"]
    assert all(saved[key] == value for key, value in scope.items())
    assert saved["catalog_files_prepared"] == 2
    if state == "catalogs":
        assert saved == original  # Still needed to resume preparation without refetching.
    else:
        assert not set(caches).intersection(saved)
        assert len(json.dumps(saved)) < 1024
    assert set(caches).issubset(original)  # Do not mutate shared recovery state.
