"""Deterministic filesystem fencing between ComicInfo enrichment and rollback."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.import_job import (
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services import import_comicinfo_enrichment as enrichment
from pullbox.services import import_rollback_execution as rollback
from pullbox.services.import_comicinfo_enrichment import PreparedComicInfoEnrichment

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
async def async_engine(tmp_path):
    """Give concurrent progress readers and rollback writers separate connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def _seed_rollback_job(
    session: AsyncSession,
    tmp_path: Path,
    *,
    status: ImportJobStatus,
) -> int:
    job = ImportJob(
        source_path=str(tmp_path / "source"),
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        import_started_at=datetime.now(UTC),
    )
    action = ImportJobAction(
        import_job=job,
        sequence_no=1,
        phase="files",
        action_type="file_imported",
        payload={},
    )
    session.add(action)
    await session.commit()
    return int(job.id)


async def _run_rollback(
    session: AsyncSession,
    job_id: int,
    *,
    rollback_action: Any,
) -> bool:
    return await rollback.rollback_import_job(
        session,
        job_id,
        rollback_action=rollback_action,
        restore_review_state=AsyncMock(),
        recompute_series_counters=AsyncMock(),
        recompute_file_counters=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=lambda _job: {},
    )


def _prepared(path: Path) -> PreparedComicInfoEnrichment:
    return PreparedComicInfoEnrichment(
        artifact_path=path,
        payload={"Series": "Race", "Number": "1"},
        library_file_id=1,
        library_file_name=path.name,
        issue_id=1,
        issue_cv_id=None,
    )


def _install_enrichment_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prepared: PreparedComicInfoEnrichment,
    prepare: Any | None = None,
) -> None:
    async def load_pending_ids(_factory: object, *, job_id: int) -> list[int]:
        assert job_id > 0
        return [17]

    async def prepare_pending(
        _factory: object,
        *,
        imported_file_id: int,
        build_comicinfo_payload: object,
    ) -> PreparedComicInfoEnrichment:
        _ = build_comicinfo_payload
        assert imported_file_id == 17
        return prepared

    async def mark_complete(*_args: object, **_kwargs: object) -> bool:
        return True

    async def mark_failed(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(enrichment, "_load_pending_imported_file_ids", load_pending_ids)
    monkeypatch.setattr(
        enrichment,
        "_prepare_pending_imported_file_with_retry",
        prepare or prepare_pending,
    )
    monkeypatch.setattr(enrichment, "_mark_pending_file_complete_with_retry", mark_complete)
    monkeypatch.setattr(enrichment, "_mark_pending_file_failed", mark_failed)


async def _unused_build(*_args: object, **_kwargs: object) -> dict[str, Any]:
    raise AssertionError("prepared enrichment should bypass payload construction")


async def _unused_log(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("completion persistence is stubbed for the race test")


@pytest.mark.asyncio
async def test_rollback_first_blocks_queued_enrichment_and_it_exits_without_mutation(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrichment.reset_comicinfo_enrichment_gate()
    job_id = await _seed_rollback_job(
        db_session,
        tmp_path,
        status=ImportJobStatus.ROLLING_BACK,
    )
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    artifact = tmp_path / "rollback-first.cbz"
    artifact.write_text("original")
    _install_enrichment_stubs(monkeypatch, prepared=_prepared(artifact))

    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    applied = asyncio.Event()

    async def rollback_action(_session: AsyncSession, _action: object) -> None:
        rollback_started.set()
        await release_rollback.wait()

    async def apply_comicinfo(_path: Path, _payload: dict[str, Any]) -> None:
        applied.set()

    rollback_task = asyncio.create_task(
        _run_rollback(db_session, job_id, rollback_action=rollback_action)
    )
    await asyncio.wait_for(rollback_started.wait(), timeout=1)
    enrichment_task = asyncio.create_task(
        enrichment.run_import_comicinfo_enrichment(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=_unused_build,
            apply_comicinfo=apply_comicinfo,
            log_event=_unused_log,
        )
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(applied.wait(), timeout=0.05)
    release_rollback.set()
    assert await rollback_task is True
    await enrichment_task

    assert not applied.is_set()
    assert artifact.read_text() == "original"


@pytest.mark.asyncio
async def test_enrichment_mutation_first_finishes_before_rollback_touches_artifact(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrichment.reset_comicinfo_enrichment_gate()
    job_id = await _seed_rollback_job(
        db_session,
        tmp_path,
        status=ImportJobStatus.COMPLETED,
    )
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    artifact = tmp_path / "enrichment-first.cbz"
    artifact.write_text("original")
    _install_enrichment_stubs(monkeypatch, prepared=_prepared(artifact))

    apply_started = asyncio.Event()
    release_apply = asyncio.Event()
    apply_finished = asyncio.Event()
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    overlap_detected = False

    async def apply_comicinfo(path: Path, _payload: dict[str, Any]) -> None:
        nonlocal overlap_detected
        apply_started.set()
        await release_apply.wait()
        overlap_detected = overlap_detected or rollback_started.is_set()
        path.write_text("enriched")
        apply_finished.set()

    enrichment_task = asyncio.create_task(
        enrichment.run_import_comicinfo_enrichment(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=_unused_build,
            apply_comicinfo=apply_comicinfo,
            log_event=_unused_log,
        )
    )
    await asyncio.wait_for(apply_started.wait(), timeout=1)
    async with session_factory() as status_session:
        durable_job = await status_session.get(ImportJob, job_id)
        assert durable_job is not None
        durable_job.status = ImportJobStatus.ROLLING_BACK
        await status_session.commit()
    db_session.expire_all()

    async def rollback_action(_session: AsyncSession, _action: object) -> None:
        nonlocal overlap_detected
        overlap_detected = overlap_detected or not apply_finished.is_set()
        rollback_started.set()
        await release_rollback.wait()

    rollback_task = asyncio.create_task(
        _run_rollback(db_session, job_id, rollback_action=rollback_action)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(rollback_started.wait(), timeout=0.05)

    release_apply.set()
    await enrichment_task
    await asyncio.wait_for(rollback_started.wait(), timeout=1)
    release_rollback.set()
    assert await rollback_task is True

    assert overlap_detected is False
    assert artifact.read_text() == "enriched"


@pytest.mark.asyncio
async def test_rollback_requested_after_prepare_is_rechecked_before_mutation(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrichment.reset_comicinfo_enrichment_gate()
    job_id = await _seed_rollback_job(
        db_session,
        tmp_path,
        status=ImportJobStatus.COMPLETED,
    )
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    artifact = tmp_path / "prepared-before-request.cbz"
    artifact.write_text("original")
    prepared = _prepared(artifact)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    applied = asyncio.Event()

    async def prepare_pending(*_args: object, **_kwargs: object) -> PreparedComicInfoEnrichment:
        prepare_started.set()
        await release_prepare.wait()
        return prepared

    _install_enrichment_stubs(
        monkeypatch,
        prepared=prepared,
        prepare=prepare_pending,
    )

    async def apply_comicinfo(_path: Path, _payload: dict[str, Any]) -> None:
        applied.set()

    enrichment_task = asyncio.create_task(
        enrichment.run_import_comicinfo_enrichment(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=_unused_build,
            apply_comicinfo=apply_comicinfo,
            log_event=_unused_log,
        )
    )
    await asyncio.wait_for(prepare_started.wait(), timeout=1)
    async with session_factory() as status_session:
        durable_job = await status_session.get(ImportJob, job_id)
        assert durable_job is not None
        durable_job.status = ImportJobStatus.ROLLING_BACK
        await status_session.commit()
    release_prepare.set()
    await enrichment_task

    assert not applied.is_set()
    assert artifact.read_text() == "original"
