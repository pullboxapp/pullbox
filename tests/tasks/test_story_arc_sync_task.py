"""Task-level contracts for draining durable story-arc synchronization work."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.scheduler import PullboxScheduler, get_registered_tasks
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services import story_arc_placement_integration as placement_integration
from pullbox.services import story_arc_sync_queue as sync_queue
from pullbox.services.import_service import RunImportResult
from pullbox.services.import_story_arc_placement_completion import (
    finalize_import_story_arc_placements,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncResult,
)
from pullbox.services.story_arc_sync_queue import (
    STORY_ARC_SYNC_TASK_ID,
    StoryArcSyncDrainResult,
    claim_story_arc_sync_work,
    enqueue_story_arc_sync_work,
    process_story_arc_sync_work,
)
from pullbox.tasks import story_arc_sync_task as sync_task_module
from pullbox.tasks.import_task import ImportRunner
from pullbox.tasks.story_arc_sync_task import scheduled_sync_story_arc_placements

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'arc-sync-task.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_two_work_items(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int]:
    canonical = tmp_path / "library" / "Batman.cbz"
    canonical.parent.mkdir(exist_ok=True)
    canonical.write_bytes(b"canonical")
    arc_root = tmp_path / "arcs"
    arc_root.mkdir(exist_ok=True)
    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(canonical.parent), enabled=True)
        series = Series(title="Batman", sort_title="batman", library_root=root)
        issue = Issue(series=series, issue_number=1, issue_number_text="1")
        library_file = LibraryFile(
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
            source_signature={"size": canonical.stat().st_size, "mtime_ns": 1},
        )
        session.add(library_file)
        await session.flush()
        memberships: list[IssueStoryArc] = []
        for index in (1, 2):
            arc = StoryArc(
                name=f"Arc {index}",
                source_kind=StoryArcSourceKind.PULLBOX,
                sync_enabled=True,
                target_library_root_id=root.id,
                policy_schema_version=1,
                policy_snapshot={
                    "schema_version": 1,
                    "mode": "copy",
                    "target_library_root_id": root.id,
                    "destination_root": str(arc_root),
                    "folder_template": "{StoryArc}",
                    "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
                    "symlink_style": None,
                    "synchronize": True,
                },
                revision=2,
            )
            memberships.append(
                IssueStoryArc(
                    story_arc=arc,
                    issue=issue,
                    sequence_number=index,
                    source_ordinal=index,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                    source_kind=StoryArcSourceKind.PULLBOX,
                    sync_eligible=True,
                )
            )
        session.add_all(memberships)
        await session.flush()
        await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()
        return memberships[0].id, memberships[1].id


async def _record_action(
    session: AsyncSession,
    job: ImportJob,
    *,
    phase: str,
    action_type: str,
    payload: dict[str, Any],
) -> ImportJobAction:
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase=phase,
        action_type=action_type,
        payload=payload,
    )
    session.add(action)
    await session.flush()
    return action


def _sqlite_database_locked_error() -> SQLAlchemyOperationalError:
    return SQLAlchemyOperationalError(
        "UPDATE import_jobs SET progress_snapshot=? WHERE import_jobs.id = ?",
        {},
        sqlite3.OperationalError("database is locked"),
    )


async def _seed_origin_work(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    mode: StoryArcPlacementMode = StoryArcPlacementMode.COPY,
) -> dict[str, int | str]:
    canonical = tmp_path / f"library-{mode.value}" / "Batman.cbz"
    canonical.parent.mkdir(exist_ok=True)
    canonical.write_bytes(b"canonical import placement")
    destination = tmp_path / f"import-arcs-{mode.value}"
    destination.mkdir(exist_ok=True)
    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(canonical.parent), enabled=True)
        series = Series(title="Batman", sort_title="batman", library_root=root)
        issue = Issue(series=series, issue_number=1, issue_number_text="1")
        library_file = LibraryFile(
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
            source_signature={"size": canonical.stat().st_size, "mtime_ns": 1},
        )
        arc = StoryArc(
            name=f"Imported {mode.value}",
            source_kind=StoryArcSourceKind.MYLAR3,
            sync_enabled=False,
            target_library_root_id=None,
            policy_schema_version=1,
            policy_snapshot={},
            revision=2,
        )
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
            sync_eligible=False,
        )
        job = ImportJob(
            source_path=str(tmp_path / "mylar.db"),
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.IMPORTING,
            progress_snapshot={
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 1,
            },
        )
        staged_arc = ImportedStoryArc(
            import_job=job,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key=f"mylar3:{mode.value}",
            source_ordinal=1,
            name=arc.name,
            status=ImportedStoryArcStatus.IMPORTED,
            materialized_story_arc=arc,
        )
        entry = ImportedStoryArcEntry(
            imported_story_arc=staged_arc,
            matched_issue=issue,
            materialized_membership=membership,
            source_ordinal=1,
            reading_order=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        session.add_all([library_file, membership, entry])
        await session.flush()
        arc.target_library_root_id = root.id
        arc.policy_snapshot = {
            "schema_version": 1,
            "mode": mode.value,
            "target_library_root_id": root.id,
            "destination_root": str(destination),
            "folder_template": "{StoryArc}",
            "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
            "symlink_style": "relative" if mode is StoryArcPlacementMode.SYMLINK else None,
            "synchronize": False,
        }
        enqueued = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=arc,
            imported_story_arc_id=staged_arc.id,
            imported_story_arc_entry_id=entry.id,
            record_action=_record_action,
        )
        assert enqueued.work is not None and enqueued.action is not None
        # Import-owned rows are held until the build snapshot is durably sealed.
        enqueued.work.claimable = True
        await session.commit()
        return {
            "work": enqueued.work.id,
            "action": enqueued.action.id,
            "job": job.id,
            "membership": membership.id,
            "canonical": str(canonical),
            "destination": str(destination),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        StoryArcPlacementMode.COPY,
        StoryArcPlacementMode.HARDLINK,
        StoryArcPlacementMode.SYMLINK,
    ],
)
async def test_origin_work_publishes_exact_managed_modes_despite_automation_flags(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: StoryArcPlacementMode,
) -> None:
    ids = await _seed_origin_work(db_factory, tmp_path, mode=mode)

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
    )

    assert result.completed == 1
    assert result.import_jobs_evaluated == (int(ids["job"]),)
    assert result.import_jobs_completed == (int(ids["job"]),)
    assert result.import_jobs_stalled == ()
    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, int(ids["work"]))
        placement = await session.scalar(select(StoryArcPlacement))
        membership = await session.get(IssueStoryArc, int(ids["membership"]))
        job = await session.get(ImportJob, int(ids["job"]))
    assert work is not None and work.state is StoryArcSyncWorkState.COMPLETED
    assert placement is not None
    assert placement.mode is mode
    assert placement.ownership is StoryArcPlacementOwnership.MANAGED
    assert placement.source_import_job_id == int(ids["job"])
    assert placement.creating_action_id == int(ids["action"])
    assert membership is not None and membership.sync_eligible is False
    assert job is not None and job.status is ImportJobStatus.COMPLETED
    assert job.progress_snapshot["phase"] == "done"
    target = Path(placement.placement_path)
    assert target.exists()
    if mode is StoryArcPlacementMode.SYMLINK:
        assert target.is_symlink()
    else:
        assert target.read_bytes() == Path(str(ids["canonical"])).read_bytes()


@pytest.mark.asyncio
async def test_import_finalizer_runs_only_after_pending_origin_work_is_terminal(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _seed_origin_work(db_factory, tmp_path)
    finalized: list[int] = []

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session
        finalized.append(job_id)
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(
        sync_queue,
        "finalize_import_story_arc_placements",
        _fake_finalizer,
    )

    first = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=(int(ids["job"]),),
    )

    assert first == ((), (), ())
    assert finalized == []

    async with db_factory() as session:
        await session.execute(
            update(StoryArcSyncWork)
            .where(StoryArcSyncWork.id == int(ids["work"]))
            .values(state=StoryArcSyncWorkState.COMPLETED)
        )
        await session.commit()

    second = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=(int(ids["job"]),),
    )

    assert second == (
        (int(ids["job"]),),
        (int(ids["job"]),),
        (),
    )
    assert finalized == [int(ids["job"])]


async def _seed_waiting_import_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    status: ImportJobStatus = ImportJobStatus.IMPORTING,
) -> int:
    async with factory() as session:
        job = ImportJob(
            source_path=f"/imports/{name}",
            source_type=ImportSourceType.FILESYSTEM,
            status=status,
            control_request=ImportControlRequest.NONE,
            progress_snapshot={
                "status": status.value,
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 1,
            },
        )
        session.add(job)
        await session.commit()
        return int(job.id)


@pytest.mark.asyncio
async def test_touched_finalization_also_runs_bounded_recovery_candidates(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_job_id = await _seed_waiting_import_job(db_factory, name="recovery")
    touched_job_id = await _seed_waiting_import_job(db_factory, name="touched")
    finalized: list[int] = []

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session
        finalized.append(job_id)
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(sync_queue, "finalize_import_story_arc_placements", _fake_finalizer)

    result = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=(touched_job_id,),
    )

    assert result[0] == (touched_job_id, recovery_job_id)
    assert result[1] == (touched_job_id, recovery_job_id)
    assert result[2] == ()
    assert finalized == [touched_job_id, recovery_job_id]


@pytest.mark.asyncio
async def test_full_touched_batch_reserves_capacity_for_recovery(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_job_id = await _seed_waiting_import_job(db_factory, name="reserved-recovery")
    touched_job_ids = (
        await _seed_waiting_import_job(db_factory, name="full-touched-1"),
        await _seed_waiting_import_job(db_factory, name="full-touched-2"),
    )

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session, job_id
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(sync_queue, "MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE", 2)
    monkeypatch.setattr(sync_queue, "finalize_import_story_arc_placements", _fake_finalizer)

    evaluated, completed, stalled = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=touched_job_ids,
    )

    assert recovery_job_id in evaluated
    assert recovery_job_id in completed
    assert len(evaluated) == 2
    assert stalled == ()


@pytest.mark.asyncio
async def test_empty_touched_set_still_runs_bounded_finalizer_recovery(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_job_id = await _seed_waiting_import_job(db_factory, name="empty-touched")

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session, job_id
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(sync_queue, "finalize_import_story_arc_placements", _fake_finalizer)

    result = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=(),
    )

    assert result == ((recovery_job_id,), (recovery_job_id,), ())


@pytest.mark.asyncio
async def test_pending_head_job_cannot_starve_terminal_recovery_candidate(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_origin_work(db_factory, tmp_path)
    recovery_job_id = await _seed_waiting_import_job(db_factory, name="behind-pending")
    finalized: list[int] = []

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session
        finalized.append(job_id)
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(sync_queue, "MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE", 1)
    monkeypatch.setattr(sync_queue, "finalize_import_story_arc_placements", _fake_finalizer)

    result = await sync_queue._finalize_waiting_import_story_arc_placements(db_factory)

    assert result == ((recovery_job_id,), (recovery_job_id,), ())
    assert finalized == [recovery_job_id]


@pytest.mark.asyncio
async def test_touched_stalled_wait_is_rechecked_after_placement_retry(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled_job_id = await _seed_waiting_import_job(
        db_factory,
        name="stalled-retry",
        status=ImportJobStatus.STALLED,
    )

    async def _fake_finalizer(session: AsyncSession, job_id: int) -> Any:
        del session, job_id
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(sync_queue, "finalize_import_story_arc_placements", _fake_finalizer)

    result = await sync_queue._finalize_waiting_import_story_arc_placements(
        db_factory,
        candidate_job_ids=(stalled_job_id,),
    )

    assert result == ((stalled_job_id,), (stalled_job_id,), ())


@pytest.mark.asyncio
async def test_database_lock_after_terminal_origin_work_uses_untargeted_finalizer_recovery(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final commit lock must not require replaying the canonical import."""
    ids = await _seed_origin_work(db_factory, tmp_path)
    initial = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
    )
    assert initial.import_jobs_completed == (int(ids["job"]),)

    async with db_factory() as session:
        job = await session.get(ImportJob, int(ids["job"]))
        assert job is not None
        snapshot = dict(job.progress_snapshot or {})
        snapshot.update(
            {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "message": "Creating the approved story-arc copies and links...",
            }
        )
        job.status = ImportJobStatus.IMPORTING
        job.import_started_at = job.import_started_at or datetime.now(UTC)
        job.import_completed_at = None
        job.error_message = None
        job.story_arc_placement_followup_pending = False
        job.progress_snapshot = snapshot
        await session.commit()

    service = AsyncMock()

    async def database_only_finalize(
        session: AsyncSession,
        job_id: int,
        progress_callback: object | None = None,
    ) -> RunImportResult:
        del progress_callback
        outcome = await finalize_import_story_arc_placements(session, job_id)
        return RunImportResult(
            schedule_comicinfo_enrichment=outcome.state
            is sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED,
        )

    service.run_import.side_effect = database_only_finalize
    service.schedule_comicinfo_enrichment = MagicMock()
    service.schedule_story_arc_sync = MagicMock()
    original_commit = AsyncSession.commit
    lock_injected = False

    async def lock_first_runner_commit(session: AsyncSession) -> None:
        nonlocal lock_injected
        if not lock_injected:
            lock_injected = True
            raise _sqlite_database_locked_error()
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", lock_first_runner_commit)
    monkeypatch.setattr(
        "pullbox.tasks.import_task._build_import_service",
        AsyncMock(return_value=service),
    )
    monkeypatch.setattr("pullbox.tasks.import_task.publish", AsyncMock())

    runner = ImportRunner(db_factory)
    await runner._run_job(int(ids["job"]))

    async with db_factory() as session:
        stalled = await session.get(ImportJob, int(ids["job"]))
        assert stalled is not None
        assert stalled.status is ImportJobStatus.STALLED
        assert stalled.progress_snapshot["phase"] == "story_arc_placements"

    recovered = await sync_queue._finalize_waiting_import_story_arc_placements(db_factory)

    assert recovered == (
        (int(ids["job"]),),
        (int(ids["job"]),),
        (),
    )
    service.run_import.assert_awaited_once()
    async with db_factory() as session:
        completed = await session.get(ImportJob, int(ids["job"]))
        assert completed is not None
        assert completed.status is ImportJobStatus.COMPLETED
        assert completed.progress_snapshot["phase"] == "done"


@pytest.mark.asyncio
async def test_finalizer_backlog_beyond_bound_requests_prompt_continuation(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 101st terminal placement wait must not sleep for the base interval."""
    job_count = sync_queue.MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE + 1
    async with db_factory() as session:
        jobs = [
            ImportJob(
                source_path=f"/imports/finalizer-backlog-{index}",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.IMPORTING,
                control_request=ImportControlRequest.NONE,
                progress_snapshot={
                    "status": ImportJobStatus.IMPORTING.value,
                    "mode": "import",
                    "phase": "story_arc_placements",
                    "progress": 99,
                    "story_arc_placements_total": 1,
                },
            )
            for index in range(job_count)
        ]
        session.add_all(jobs)
        await session.commit()
        job_ids = tuple(int(job.id) for job in jobs)

    async def complete_without_origin_validation(session: AsyncSession, job_id: int) -> Any:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        job.status = ImportJobStatus.COMPLETED
        job.progress_snapshot = {
            "status": ImportJobStatus.COMPLETED.value,
            "mode": "import",
            "phase": "done",
            "progress": 100,
        }
        return type(
            "Outcome",
            (),
            {"state": sync_queue.ImportStoryArcPlacementCompletionState.COMPLETED},
        )()

    monkeypatch.setattr(
        sync_queue,
        "finalize_import_story_arc_placements",
        complete_without_origin_validation,
    )

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
    )

    assert result.import_jobs_evaluated == job_ids[:-1]
    assert result.has_more is True
    assert result.next_retry_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [
        "paused",
        "cancelled",
        "rolling_back",
        "control_cancel",
        "work_cancel_requested",
        "rolled_back_action",
        "payload_changed",
        "typed_origin_changed",
        "generation_changed",
    ],
)
async def test_invalid_or_cancelled_origin_work_never_publishes(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    invalid_state: str,
) -> None:
    ids = await _seed_origin_work(db_factory, tmp_path)
    async with db_factory() as session:
        job = await session.get(ImportJob, int(ids["job"]))
        action = await session.get(ImportJobAction, int(ids["action"]))
        work = await session.get(StoryArcSyncWork, int(ids["work"]))
        assert job is not None and action is not None and work is not None
        if invalid_state == "paused":
            job.status = ImportJobStatus.PAUSED
        elif invalid_state == "cancelled":
            job.status = ImportJobStatus.CANCELLED
        elif invalid_state == "rolling_back":
            job.status = ImportJobStatus.ROLLING_BACK
        elif invalid_state == "control_cancel":
            job.control_request = ImportControlRequest.CANCEL
        elif invalid_state == "work_cancel_requested":
            work.cancel_requested_at = datetime.now(UTC)
        elif invalid_state == "rolled_back_action":
            action.status = ImportJobActionStatus.ROLLED_BACK
        elif invalid_state == "payload_changed":
            action.payload = {**dict(action.payload), "membership_id": 999_999}
        elif invalid_state == "typed_origin_changed":
            work.origin_imported_story_arc_id = None
        else:
            work.desired_generation = "0" * 64
        await session.commit()

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
    )

    assert result.completed == 0
    assert result.cancelled == 1
    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, int(ids["work"]))
        placement_count = int(await session.scalar(select(func.count(StoryArcPlacement.id))) or 0)
    assert work is not None and work.state is StoryArcSyncWorkState.CANCELLED
    assert placement_count == 0
    assert list(Path(str(ids["destination"])).rglob("*.cbz")) == []


@pytest.mark.asyncio
async def test_cancelled_inflight_origin_work_releases_deferred_import_rollback(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ids = await _seed_origin_work(db_factory, tmp_path)
    async with db_factory() as session:
        job = await session.get(ImportJob, int(ids["job"]))
        work = await session.get(StoryArcSyncWork, int(ids["work"]))
        assert job is not None and work is not None
        job.status = ImportJobStatus.ROLLING_BACK
        job.control_request = ImportControlRequest.CANCEL
        job.progress_snapshot = {
            "mode": "rollback",
            "phase": "story_arc_placements",
            "progress": 0,
            "story_arc_rollback_waiting_work_id": "stale-json-value",
        }
        job.story_arc_rollback_waiting_work_id = work.id
        await session.commit()

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
    )

    assert result.cancelled == 1
    assert result.import_jobs_rollback_ready == (int(ids["job"]),)


@pytest.mark.asyncio
async def test_origin_work_cancelled_during_io_never_publishes(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _seed_origin_work(db_factory, tmp_path)
    started = threading.Event()
    release = threading.Event()
    execute = placement_integration.execute_story_arc_placement

    def delayed_execute(*args: Any, **kwargs: Any) -> Any:
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release placement execution")
        return execute(*args, **kwargs)

    monkeypatch.setattr(
        placement_integration,
        "execute_story_arc_placement",
        delayed_execute,
    )
    worker = asyncio.create_task(
        process_story_arc_sync_work(
            session_factory=db_factory,
            batch_size=1,
            discover=False,
            origin_cancellation_poll_seconds=0.01,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with db_factory() as session:
            work = await session.get(StoryArcSyncWork, int(ids["work"]))
            assert work is not None
            work.cancel_requested_at = datetime.now(UTC)
            await session.commit()
        await asyncio.sleep(0.05)
        release.set()
        result = await worker
    finally:
        release.set()
        if not worker.done():
            await worker

    assert result.completed == 0
    assert result.cancelled == 1
    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, int(ids["work"]))
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
    assert work is not None and work.state is StoryArcSyncWorkState.CANCELLED
    assert all(not Path(placement.placement_path).exists() for placement in placements)
    assert Path(str(ids["canonical"])).read_bytes() == b"canonical import placement"
    assert list(Path(str(ids["destination"])).rglob("*.cbz")) == []


class _OneFailureSyncService:
    def __init__(self, failed_membership_id: int) -> None:
        self.failed_membership_id = failed_membership_id
        self.calls: list[int] = []
        self.open_transactions: list[bool] = []

    async def sync_membership(
        self,
        session: AsyncSession,
        _story_arc_id: int,
        membership_id: int,
    ) -> StoryArcPlacementSyncResult:
        self.calls.append(membership_id)
        self.open_transactions.append(session.in_transaction())
        if membership_id == self.failed_membership_id:
            raise StoryArcPlacementIntegrationError(
                "placement_destination_conflict",
                "occupied",
                category="collision",
            )
        return StoryArcPlacementSyncResult(
            membership_id=membership_id,
            outcome="created",
            placement=None,
        )


class _TransientFailureSyncService(_OneFailureSyncService):
    async def sync_membership(
        self,
        session: AsyncSession,
        _story_arc_id: int,
        membership_id: int,
    ) -> StoryArcPlacementSyncResult:
        self.calls.append(membership_id)
        self.open_transactions.append(session.in_transaction())
        raise StoryArcPlacementIntegrationError(
            "placement_execution_failed",
            "temporary filesystem failure",
            category="safety",
        )


class _BlockedSyncService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def sync_membership(
        self,
        _session: AsyncSession,
        _story_arc_id: int,
        membership_id: int,
    ) -> StoryArcPlacementSyncResult:
        self.started.set()
        await self.release.wait()
        return StoryArcPlacementSyncResult(
            membership_id=membership_id,
            outcome="created",
            placement=None,
        )


class _ClaimStealingSyncService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def sync_membership(
        self,
        _session: AsyncSession,
        _story_arc_id: int,
        membership_id: int,
    ) -> StoryArcPlacementSyncResult:
        async with self.factory() as session:
            await session.execute(
                update(StoryArcSyncWork)
                .where(StoryArcSyncWork.issue_story_arc_id == membership_id)
                .values(claim_token="replacement-owner")
            )
            await session.commit()
        return StoryArcPlacementSyncResult(
            membership_id=membership_id,
            outcome="created",
            placement=None,
        )


class _ClaimStealingFailureSyncService(_ClaimStealingSyncService):
    async def sync_membership(
        self,
        session: AsyncSession,
        story_arc_id: int,
        membership_id: int,
    ) -> StoryArcPlacementSyncResult:
        await super().sync_membership(session, story_arc_id, membership_id)
        raise StoryArcPlacementIntegrationError(
            "placement_execution_failed",
            "former worker no longer owns this claim",
            category="operation",
        )


@pytest.mark.asyncio
async def test_worker_isolates_one_arc_failure_and_starts_io_without_a_transaction(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    failed_id, completed_id = await _seed_two_work_items(db_factory, tmp_path)
    service = _OneFailureSyncService(failed_id)

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        sync_service=service,
        batch_size=10,
        discover=False,
    )

    async with db_factory() as session:
        rows = {
            row.issue_story_arc_id: row
            for row in (await session.scalars(select(StoryArcSyncWork))).all()
        }
    assert result.claimed == 2
    assert result.completed == 1
    assert result.failed == 1
    assert service.calls == [failed_id, completed_id]
    assert service.open_transactions == [False, False]
    assert rows[failed_id].state is StoryArcSyncWorkState.FAILED
    assert rows[completed_id].state is StoryArcSyncWorkState.COMPLETED


@pytest.mark.asyncio
async def test_scheduled_wrapper_delegates_to_bounded_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def fake_process(*, import_only: bool = False) -> StoryArcSyncDrainResult:
        observed["called"] = True
        observed["import_only"] = import_only
        return StoryArcSyncDrainResult(
            discovered=0,
            claimed=0,
            completed=0,
            failed=0,
            retrying=0,
            cancelled=0,
            lost_claims=0,
            has_more=False,
            next_retry_at=None,
            import_jobs_evaluated=(41,),
            import_jobs_completed=(41,),
            import_jobs_rollback_ready=(42,),
        )

    async def fake_publish(
        job_ids: tuple[int, ...],
        *,
        completed_job_ids: tuple[int, ...],
    ) -> None:
        observed["published"] = (job_ids, completed_job_ids)

    def fake_trigger_rollback(job_id: int) -> None:
        observed["rollback"] = job_id

    monkeypatch.setattr(
        "pullbox.tasks.story_arc_sync_task.process_story_arc_sync_work",
        fake_process,
    )
    monkeypatch.setattr(
        sync_task_module,
        "has_active_import_scheduler_protection",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "pullbox.tasks.import_task.publish_story_arc_import_updates",
        fake_publish,
    )
    monkeypatch.setattr(
        "pullbox.tasks.import_task.trigger_import_rollback",
        fake_trigger_rollback,
    )

    await scheduled_sync_story_arc_placements()

    assert observed == {
        "called": True,
        "import_only": False,
        "published": ((41,), (41,)),
        "rollback": 42,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type", ["scheduled", "manual"])
async def test_protected_import_wrapper_runs_import_only_lifecycle_work(
    monkeypatch: pytest.MonkeyPatch,
    trigger_type: str,
) -> None:
    observed: dict[str, object] = {}

    async def fake_process(*, import_only: bool = False) -> StoryArcSyncDrainResult:
        observed["import_only"] = import_only
        return StoryArcSyncDrainResult(
            discovered=0,
            claimed=0,
            completed=0,
            failed=0,
            retrying=0,
            cancelled=0,
            lost_claims=0,
            has_more=False,
            next_retry_at=None,
            import_jobs_evaluated=(41,),
            import_jobs_completed=(41,),
            import_jobs_rollback_ready=(42,),
        )

    async def fake_publish(
        job_ids: tuple[int, ...],
        *,
        completed_job_ids: tuple[int, ...],
    ) -> None:
        observed["published"] = (job_ids, completed_job_ids)

    def fake_trigger_rollback(job_id: int) -> None:
        observed["rollback"] = job_id

    core_protection = AsyncMock(return_value=True)
    task_protection = AsyncMock(return_value=True)
    continuation_scheduler = MagicMock()
    monkeypatch.setattr(
        "pullbox.core.scheduler.has_active_import_scheduler_protection",
        core_protection,
    )
    monkeypatch.setattr(
        sync_task_module,
        "has_active_import_scheduler_protection",
        task_protection,
    )
    monkeypatch.setattr(sync_task_module, "process_story_arc_sync_work", fake_process)
    monkeypatch.setattr(sync_task_module, "get_scheduler", lambda: continuation_scheduler)
    monkeypatch.setattr(
        "pullbox.tasks.import_task.publish_story_arc_import_updates",
        fake_publish,
    )
    monkeypatch.setattr(
        "pullbox.tasks.import_task.trigger_import_rollback",
        fake_trigger_rollback,
    )
    wrapper_scheduler = PullboxScheduler()
    wrapper_scheduler._persist_task_stat = AsyncMock()  # type: ignore[method-assign]

    wrapped = wrapper_scheduler._wrap_task(
        scheduled_sync_story_arc_placements,
        STORY_ARC_SYNC_TASK_ID,
        trigger_type=trigger_type,
    )
    await wrapped()

    assert observed == {
        "import_only": True,
        "published": ((41,), (41,)),
        "rollback": 42,
    }
    core_protection.assert_not_awaited()
    task_protection.assert_awaited_once_with()
    continuation_scheduler.clear_task_continuation.assert_called_once_with(STORY_ARC_SYNC_TASK_ID)


@pytest.mark.asyncio
async def test_import_only_drain_skips_ordinary_lanes_without_starving_import(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_membership_ids = await _seed_two_work_items(db_factory, tmp_path)
    origin_ids = await _seed_origin_work(db_factory, tmp_path)
    retry_at = datetime.now(UTC) + timedelta(hours=1)
    async with db_factory() as session:
        ordinary_rows = list(
            (
                await session.scalars(
                    select(StoryArcSyncWork)
                    .where(StoryArcSyncWork.origin_import_job_id.is_(None))
                    .order_by(StoryArcSyncWork.id.asc())
                )
            ).all()
        )
        assert len(ordinary_rows) == 2
        ordinary_rows[0].state = StoryArcSyncWorkState.RETRY_WAIT
        ordinary_rows[0].next_attempt_at = retry_at
        await session.commit()

    discovery = AsyncMock(side_effect=AssertionError("import-only drain ran discovery"))
    monkeypatch.setattr(sync_queue, "discover_story_arc_sync_work", discovery)

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=True,
        import_only=True,
    )

    assert result.discovered == 0
    assert result.claimed == 1
    assert result.completed == 1
    assert result.has_more is False
    assert result.next_retry_at is None
    assert result.import_jobs_evaluated == (int(origin_ids["job"]),)
    assert result.import_jobs_completed == (int(origin_ids["job"]),)
    discovery.assert_not_awaited()
    async with db_factory() as session:
        ordinary_by_membership = {
            row.issue_story_arc_id: row
            for row in (
                await session.scalars(
                    select(StoryArcSyncWork).where(StoryArcSyncWork.origin_import_job_id.is_(None))
                )
            ).all()
        }
        origin_work = await session.get(StoryArcSyncWork, int(origin_ids["work"]))
    assert ordinary_by_membership[ordinary_membership_ids[0]].state is (
        StoryArcSyncWorkState.RETRY_WAIT
    )
    assert ordinary_by_membership[ordinary_membership_ids[1]].state is (
        StoryArcSyncWorkState.QUEUED
    )
    assert origin_work is not None
    assert origin_work.state is StoryArcSyncWorkState.COMPLETED


@pytest.mark.asyncio
async def test_import_only_drain_fences_later_startup_recovered_placement_job(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A later recovered job stays queued until that exact job is actively resumed."""
    active_ids = await _seed_origin_work(
        db_factory,
        tmp_path,
        mode=StoryArcPlacementMode.COPY,
    )
    paused_ids = await _seed_origin_work(
        db_factory,
        tmp_path,
        mode=StoryArcPlacementMode.HARDLINK,
    )
    async with db_factory() as session:
        paused_job = await session.get(ImportJob, int(paused_ids["job"]))
        assert paused_job is not None
        paused_job.status = ImportJobStatus.PAUSED
        paused_job.progress_snapshot = {
            **dict(paused_job.progress_snapshot or {}),
            "status": ImportJobStatus.PAUSED.value,
            "pause_reason": "startup_recovery",
            "recovered_status": ImportJobStatus.IMPORTING.value,
        }
        await session.commit()

    first = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=2,
        discover=False,
        import_only=True,
    )

    assert first.claimed == 1
    assert first.completed == 1
    assert first.cancelled == 0
    assert first.import_jobs_completed == (int(active_ids["job"]),)
    async with db_factory() as session:
        active_job = await session.get(ImportJob, int(active_ids["job"]))
        paused_work = await session.get(StoryArcSyncWork, int(paused_ids["work"]))
        paused_job = await session.get(ImportJob, int(paused_ids["job"]))
        assert active_job is not None
        active_job.story_arc_placement_followup_pending = False
        assert paused_work is not None
        assert paused_work.state is StoryArcSyncWorkState.QUEUED
        assert paused_job is not None
        assert paused_job.status is ImportJobStatus.PAUSED
        snapshot = dict(paused_job.progress_snapshot or {})
        snapshot.pop("pause_reason", None)
        snapshot.pop("recovered_status", None)
        snapshot["status"] = ImportJobStatus.IMPORTING.value
        paused_job.status = ImportJobStatus.IMPORTING
        paused_job.progress_snapshot = snapshot
        await session.commit()

    second = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=1,
        discover=False,
        import_only=True,
    )

    assert second.claimed == 1
    assert second.completed == 1
    assert second.cancelled == 0
    assert second.import_jobs_completed == (int(paused_ids["job"]),)


@pytest.mark.asyncio
async def test_worker_bounds_the_batch_and_persists_retry_backoff(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    first_id, second_id = await _seed_two_work_items(db_factory, tmp_path)
    service = _TransientFailureSyncService(first_id)

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        sync_service=service,
        batch_size=1,
        discover=False,
    )

    async with db_factory() as session:
        rows = {
            row.issue_story_arc_id: row
            for row in (await session.scalars(select(StoryArcSyncWork))).all()
        }
    assert result.claimed == 1
    assert result.retrying == 1
    assert result.failed == 0
    assert result.has_more is True
    assert service.calls == [first_id]
    assert rows[first_id].state is StoryArcSyncWorkState.RETRY_WAIT
    assert rows[first_id].next_attempt_at is not None
    assert rows[second_id].state is StoryArcSyncWorkState.QUEUED


def test_story_arc_sync_task_is_registered() -> None:
    task_ids = {task.task_id for task in get_registered_tasks()}

    assert "sync_story_arc_placements" in task_ids


@pytest.mark.asyncio
async def test_live_worker_heartbeats_prevent_stale_claim_recovery(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    first_id, _second_id = await _seed_two_work_items(db_factory, tmp_path)
    async with db_factory() as session:
        second = await session.scalar(
            select(StoryArcSyncWork).where(StoryArcSyncWork.issue_story_arc_id != first_id)
        )
        assert second is not None
        second.state = StoryArcSyncWorkState.COMPLETED
        await session.commit()
        work_id = int(
            await session.scalar(
                select(StoryArcSyncWork.id).where(StoryArcSyncWork.issue_story_arc_id == first_id)
            )
        )

    started_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    heartbeat_at = datetime(2026, 8, 30, 12, 14, tzinfo=UTC)
    service = _BlockedSyncService()
    worker = asyncio.create_task(
        process_story_arc_sync_work(
            session_factory=db_factory,
            sync_service=service,
            batch_size=1,
            discover=False,
            now_fn=lambda: started_at,
            heartbeat_interval_seconds=0.01,
            heartbeat_now_fn=lambda: heartbeat_at,
        )
    )
    await service.started.wait()
    await asyncio.sleep(0.03)

    async with db_factory() as session:
        stolen = await claim_story_arc_sync_work(
            session,
            work_id,
            now=started_at.replace(minute=16),
        )
    service.release.set()
    result = await worker

    assert stolen is None
    assert result.completed == 1


@pytest.mark.asyncio
async def test_lost_claim_is_not_counted_as_completed(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    first_id, _second_id = await _seed_two_work_items(db_factory, tmp_path)
    async with db_factory() as session:
        second = await session.scalar(
            select(StoryArcSyncWork).where(StoryArcSyncWork.issue_story_arc_id != first_id)
        )
        assert second is not None
        second.state = StoryArcSyncWorkState.COMPLETED
        await session.commit()

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        sync_service=_ClaimStealingSyncService(db_factory),
        batch_size=1,
        discover=False,
    )

    async with db_factory() as session:
        work = await session.scalar(
            select(StoryArcSyncWork).where(StoryArcSyncWork.issue_story_arc_id == first_id)
        )
    assert result.completed == 0
    assert result.lost_claims == 1
    assert work is not None
    assert work.state is StoryArcSyncWorkState.RUNNING
    assert work.claim_token == "replacement-owner"


@pytest.mark.asyncio
async def test_lost_claim_is_not_counted_as_failed_or_retrying(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    first_id, _second_id = await _seed_two_work_items(db_factory, tmp_path)
    async with db_factory() as session:
        second = await session.scalar(
            select(StoryArcSyncWork).where(StoryArcSyncWork.issue_story_arc_id != first_id)
        )
        assert second is not None
        second.state = StoryArcSyncWorkState.COMPLETED
        await session.commit()

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        sync_service=_ClaimStealingFailureSyncService(db_factory),
        batch_size=1,
        discover=False,
    )

    assert result.failed == 0
    assert result.retrying == 0
    assert result.lost_claims == 1


@pytest.mark.asyncio
async def test_lost_claim_is_not_counted_as_cancelled(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id, _second_id = await _seed_two_work_items(db_factory, tmp_path)
    async with db_factory() as session:
        second = await session.scalar(
            select(StoryArcSyncWork).where(StoryArcSyncWork.issue_story_arc_id != first_id)
        )
        assert second is not None
        second.state = StoryArcSyncWorkState.COMPLETED
        await session.commit()

    async def lose_claim_while_loading(
        session: AsyncSession,
        work_id: int,
        _claim_token: str,
    ) -> None:
        await session.execute(
            update(StoryArcSyncWork)
            .where(StoryArcSyncWork.id == work_id)
            .values(claim_token="replacement-owner")
        )
        await session.commit()
        return None

    monkeypatch.setattr(
        "pullbox.services.story_arc_sync_queue._load_claimed_context",
        lose_claim_while_loading,
    )

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        sync_service=_OneFailureSyncService(first_id),
        batch_size=1,
        discover=False,
    )

    assert result.cancelled == 0
    assert result.lost_claims == 1
