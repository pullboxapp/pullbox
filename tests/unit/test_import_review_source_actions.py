"""Review repairs are explicit, durable, and cannot become silent skips."""

import zipfile
from pathlib import Path
from threading import get_ident

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_review_source_actions import process_source_action, queue_source_action


async def make_source(session, root="/comics", category="archive_inspection_failed"):
    job = ImportJob(
        source_path=root, source_type=ImportSourceType.FILESYSTEM, status=ImportJobStatus.REVIEW
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id, raw_series_name="Example", cv_id=10, status=ImportSeriesStatus.MATCHED
    )
    session.add(series)
    await session.flush()
    file = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"{root}/Example 01.cbz",
        file_name="Example 01.cbz",
        file_format="cbz",
        file_size=100,
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={"safety_block": {"category": category, "overrideable": False}},
    )
    session.add(file)
    await session.flush()
    return job, series, file


async def test_recheck_saves_intent_without_approving_or_skipping(db_session: AsyncSession):
    job, series, file = await make_source(db_session)
    await queue_source_action(db_session, job.id, file.id)
    assert file.diagnostics["review_source_action"]["state"] == "pending"
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED
    assert series.diagnostics["rematch_pending"] is True


async def test_recheck_cannot_override_dangerous_content(db_session: AsyncSession):
    job, _, file = await make_source(db_session, category="dangerous_path_or_payload")
    with pytest.raises(ValidationError):
        await queue_source_action(db_session, job.id, file.id)
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED


async def test_pair_requires_a_unique_proven_replacement(db_session: AsyncSession):
    job, _, file = await make_source(db_session, category="source_missing")
    with pytest.raises(ValidationError, match="replacement"):
        await queue_source_action(db_session, job.id, file.id, action="pair")


async def test_live_recheck_preserves_source_and_rematches_only_after_inspection(
    db_session, async_engine, tmp_path, monkeypatch
):
    job, series, file = await make_source(db_session, root=str(tmp_path))
    path = Path(file.file_path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("01.jpg", b"first")
        archive.writestr("02.jpg", b"second")
    before = path.read_bytes()
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()
    event_loop_thread = get_ident()
    resolution_threads = []
    original_resolve = Path.resolve

    def record_resolution(path, *args, **kwargs):
        if path == tmp_path:
            resolution_threads.append(get_ident())
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolution)
    result = await process_source_action(
        async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
    )
    assert result == series.id
    assert resolution_threads and event_loop_thread not in resolution_threads
    await db_session.refresh(file)
    assert file.status is ImportedFileStatus.SAFETY_APPROVED
    assert file.diagnostics["review_source_action"]["state"] == "matching"
    # A restart between inspection and matching must resume the same work.
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        == series.id
    )
    assert "safety_exception" not in file.diagnostics
    assert path.read_bytes() == before


async def test_changed_review_cannot_be_overwritten_by_recheck(
    db_session, async_engine, tmp_path, monkeypatch
):
    from pullbox.services import import_review_source_actions as actions

    job, series, file = await make_source(db_session, root=str(tmp_path))
    await queue_source_action(db_session, job.id, file.id)
    file.status = ImportedFileStatus.SKIPPED
    await db_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("A stale review must not inspect files")

    monkeypatch.setattr(actions, "inspect_review_source", forbidden)
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        is None
    )
    await db_session.refresh(file)
    await db_session.refresh(series)
    assert file.status is ImportedFileStatus.SKIPPED
    assert file.diagnostics["review_source_action"]["state"] == "failed"
    assert not series.diagnostics.get("rematch_pending")


async def test_recheck_does_not_clear_new_dangerous_content(db_session, async_engine, tmp_path):
    job, _, file = await make_source(db_session, root=str(tmp_path))
    with zipfile.ZipFile(file.file_path, "w") as archive:
        archive.writestr("../escape.jpg", b"danger")
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        is None
    )
    await db_session.refresh(file)
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED
    assert file.diagnostics["safety_block"]["category"] == "dangerous_path_or_payload"


async def test_unexpected_source_worker_failure_releases_review(
    db_session, async_engine, monkeypatch
):
    from pullbox.services import import_review_source_actions as actions
    from pullbox.tasks import import_task

    job, series, file = await make_source(db_session)
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()

    async def fail(*args, **kwargs):
        raise RuntimeError("inspection service unavailable")

    monkeypatch.setattr(actions, "process_source_action", fail)
    monkeypatch.setattr(
        import_task,
        "get_session_factory",
        lambda: async_sessionmaker(async_engine, expire_on_commit=False),
    )
    await import_task.run_import_review_source_action(job.id, file.id)
    await db_session.refresh(file)
    await db_session.refresh(series)
    assert file.diagnostics["review_source_action"]["state"] == "failed"
    assert not series.diagnostics.get("rematch_pending")
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED


@pytest.mark.parametrize("change", [None, "replacement_changed", "recorded_reappeared", "cancel"])
async def test_exact_stale_pair_rechecks_both_paths(db_session, async_engine, tmp_path, change):
    from pullbox.models.import_job import ImportControlRequest
    from tests.unit.test_import_path_reconciliation import _saved

    job, _series, recorded, actual = await _saved(db_session, tmp_path)
    job.mylar3_path_map = {"/old": str(tmp_path)}
    recorded.diagnostics = {**recorded.diagnostics, "safety_block": {"category": "source_missing"}}
    before = Path(actual.file_path).read_bytes()
    await queue_source_action(db_session, job.id, recorded.id, action="pair")
    if change == "replacement_changed":
        Path(actual.file_path).write_bytes(b"changed")
    elif change == "recorded_reappeared":
        Path(recorded.file_path).write_bytes(before)
    elif change == "cancel":
        job.control_request = ImportControlRequest.CANCEL
    await db_session.commit()
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, recorded.id
        )
        is None
    )
    await db_session.refresh(recorded)
    await db_session.refresh(actual)
    if change:
        assert recorded.status is ImportedFileStatus.SAFETY_BLOCKED
        assert recorded.diagnostics["review_source_action"]["state"] == "failed"
    else:
        assert recorded.status is ImportedFileStatus.SKIPPED
        assert recorded.match_method == "verified_path_reconciliation"
        assert recorded.diagnostics["mylar3_path_reconciliation"]["actual_path"] == actual.file_path
    assert actual.status is ImportedFileStatus.MATCHED
    if change != "replacement_changed":
        assert Path(actual.file_path).read_bytes() == before
