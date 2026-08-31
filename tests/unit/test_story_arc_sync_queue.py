"""Durable outbox contracts for automatic story-arc synchronization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.exceptions import ValidationError
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportJob,
    ImportJobAction,
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
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services import story_arc_sync_queue as sync_queue
from pullbox.services.story_arc_sync_queue import (
    claim_story_arc_sync_work,
    discover_story_arc_sync_work,
    enqueue_story_arc_sync_work,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'arc-sync.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _policy_snapshot(
    root_id: int,
    destination: Path,
    *,
    mode: str = "copy",
    synchronize: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "target_library_root_id": root_id,
        "destination_root": str(destination),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
        "symlink_style": "relative" if mode == "symlink" else None,
        "synchronize": synchronize,
    }


async def _seed_canonical_file(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int]:
    canonical = tmp_path / "library" / "Batman 001.cbz"
    canonical.parent.mkdir(exist_ok=True)
    canonical.write_bytes(b"canonical")
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
        await session.commit()
        return issue.id, library_file.id


async def _add_membership(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    issue_id: int,
    name: str,
    lifecycle: StoryArcLifecycle = StoryArcLifecycle.ACTIVE,
    sync_enabled: bool = True,
    sync_eligible: bool = True,
    resolution_state: StoryArcResolutionState = StoryArcResolutionState.RESOLVED,
    configured: bool = True,
    sequence_number: int = 1,
    policy_synchronize: bool = True,
) -> int:
    destination = tmp_path / "library" / f"StoryArcs-{name}"
    destination.mkdir(exist_ok=True)
    async with factory() as session:
        root_id = int(
            await session.scalar(
                select(LibraryFile.library_root_id).where(LibraryFile.issue_id == issue_id)
            )
        )
        arc = StoryArc(
            name=name,
            source_kind=StoryArcSourceKind.PULLBOX,
            lifecycle=lifecycle,
            sync_enabled=sync_enabled,
            target_library_root_id=root_id if configured else None,
            policy_schema_version=1 if configured else None,
            policy_snapshot=(
                _policy_snapshot(
                    root_id,
                    destination,
                    synchronize=policy_synchronize,
                )
                if configured
                else {}
            ),
            revision=2,
        )
        membership = IssueStoryArc(
            story_arc=arc,
            issue_id=issue_id,
            sequence_number=sequence_number,
            source_ordinal=sequence_number,
            resolution_state=resolution_state,
            source_kind=StoryArcSourceKind.PULLBOX,
            sync_eligible=sync_eligible,
        )
        session.add(membership)
        await session.commit()
        return membership.id


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
        sequence_no=int(
            await session.scalar(
                select(func.count(ImportJobAction.id)).where(
                    ImportJobAction.import_job_id == job.id
                )
            )
            or 0
        )
        + 1,
        phase=phase,
        action_type=action_type,
        payload=payload,
    )
    session.add(action)
    await session.flush()
    return action


async def _seed_import_enqueue_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int, int, int, int]:
    issue_id, library_file_id = await _seed_canonical_file(factory, tmp_path)
    membership_id = await _add_membership(
        factory,
        tmp_path,
        issue_id=issue_id,
        name="Import managed placement",
        sync_enabled=False,
        sync_eligible=False,
        policy_synchronize=False,
    )
    async with factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        job = ImportJob(
            source_path=str(tmp_path / "mylar.db"),
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.IMPORTING,
            progress_snapshot={"phase": "story_arc_placements"},
        )
        staged_arc = ImportedStoryArc(
            import_job=job,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:arc:1",
            source_ordinal=1,
            name="Import managed placement",
            status=ImportedStoryArcStatus.IMPORTED,
            materialized_story_arc_id=membership.story_arc_id,
        )
        entry = ImportedStoryArcEntry(
            imported_story_arc=staged_arc,
            matched_issue_id=issue_id,
            materialized_membership_id=membership_id,
            source_ordinal=1,
            reading_order=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        session.add(entry)
        await session.commit()
        return job.id, staged_arc.id, entry.id, membership_id, library_file_id


async def _seed_terminal_import_sync_work(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    state: StoryArcSyncWorkState,
) -> tuple[int, int, int]:
    """Create one exact import-owned placement row in a retryable terminal state."""
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(factory, tmp_path)
    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        library_file = await session.get(LibraryFile, library_file_id)
        membership = await session.get(IssueStoryArc, membership_id)
        assert job is not None and library_file is not None and membership is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        created = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        assert created.work is not None and created.action is not None
        created.work.claimable = True
        created.work.state = state
        created.work.attempt_count = 5
        created.work.last_error_code = "placement_failed"
        created.work.last_error_category = "operation"
        created.work.last_error_detail = "stale failure detail"
        created.work.last_result = {"schema_version": 1, "status": "stale"}
        job.status = ImportJobStatus.STALLED
        job.import_started_at = datetime.now(UTC)
        job.error_message = "Story-arc placement work stalled."
        job.progress_snapshot = {
            "status": ImportJobStatus.STALLED.value,
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
            "message": "Story-arc placement work stalled.",
            "story_arc_placements_total": 1,
            "story_arc_placements_queued": 0,
            "story_arc_placements_running": 0,
            "story_arc_placements_retry_wait": 0,
            "story_arc_placements_failed": int(state is StoryArcSyncWorkState.FAILED),
            "story_arc_placements_completed": 0,
            "story_arc_placements_cancelled": int(state is StoryArcSyncWorkState.CANCELLED),
        }
        await session.commit()
        return job_id, int(created.work.id), int(created.action.id)


async def _seed_ready_selection_work(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    now: datetime,
) -> dict[str, int]:
    """Create every queue state needed to exercise the indexed ready lanes."""
    issue_id, library_file_id = await _seed_canonical_file(factory, tmp_path)
    membership_id = await _add_membership(
        factory,
        tmp_path,
        issue_id=issue_id,
        name="Ready selection",
    )
    specifications = (
        (
            "unclaimed_running",
            StoryArcSyncWorkState.RUNNING,
            now - timedelta(days=3),
            None,
            None,
        ),
        (
            "queued_older",
            StoryArcSyncWorkState.QUEUED,
            now - timedelta(hours=2),
            None,
            None,
        ),
        (
            "due_retry",
            StoryArcSyncWorkState.RETRY_WAIT,
            now - timedelta(days=5),
            now - timedelta(minutes=90),
            None,
        ),
        (
            "stale_running",
            StoryArcSyncWorkState.RUNNING,
            now - timedelta(days=4),
            None,
            now - timedelta(minutes=30),
        ),
        (
            "queued_newer",
            StoryArcSyncWorkState.QUEUED,
            now - timedelta(minutes=10),
            None,
            None,
        ),
        (
            "future_retry",
            StoryArcSyncWorkState.RETRY_WAIT,
            now - timedelta(days=6),
            now + timedelta(minutes=1),
            None,
        ),
        (
            "fresh_running",
            StoryArcSyncWorkState.RUNNING,
            now - timedelta(days=7),
            None,
            now - timedelta(minutes=5),
        ),
        (
            "completed",
            StoryArcSyncWorkState.COMPLETED,
            now - timedelta(days=8),
            None,
            None,
        ),
    )
    async with factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        library_file = await session.get(LibraryFile, library_file_id)
        assert membership is not None and library_file is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        ids: dict[str, int] = {}
        for name, state, created_at, next_attempt_at, claimed_at in specifications:
            work = StoryArcSyncWork(
                issue_story_arc_id=membership.id,
                library_file_id=library_file.id,
                desired_generation=name,
                source_signature_hash=f"signature-{name}",
                source_file_path=library_file.file_path,
                source_file_size=library_file.file_size,
                source_file_modified_at=library_file.file_modified_at,
                story_arc_revision=story_arc.revision,
                membership_sequence=membership.sequence_number,
                policy_schema_version=story_arc.policy_schema_version or 1,
                state=state,
                next_attempt_at=next_attempt_at,
                claimed_at=claimed_at,
                created_at=created_at,
            )
            session.add(work)
            await session.flush()
            ids[name] = work.id
        await session.commit()
        return ids


@pytest.mark.asyncio
async def test_import_enqueue_creates_one_exact_origin_binding_and_recovers_it_pending(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(db_factory, tmp_path)

    async with db_factory() as session:
        job = await session.get(ImportJob, job_id)
        library_file = await session.get(LibraryFile, library_file_id)
        membership = await session.get(IssueStoryArc, membership_id)
        assert job is not None and library_file is not None and membership is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        created = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        reused = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        await session.commit()

    assert created.classification == "created"
    assert created.work is not None and created.action is not None
    assert created.work.origin_import_action_id == created.action.id
    assert created.work.origin_import_job_id == job_id
    assert created.work.origin_imported_story_arc_id == staged_arc_id
    assert created.work.origin_imported_story_arc_entry_id == entry_id
    assert reused.classification == "existing_import_work_pending"
    assert reused.work is not None and reused.work.id == created.work.id
    assert reused.action is not None and reused.action.id == created.action.id
    assert created.action.action_type == "story_arc_managed_placement_requested"
    assert created.action.phase == "story_arc_placements"
    assert created.action.payload == {
        "schema_version": 1,
        "sync_work_id": created.work.id,
        "membership_id": membership_id,
        "desired_generation": created.work.desired_generation,
        "imported_story_arc_id": staged_arc_id,
        "imported_story_arc_entry_id": entry_id,
        "source_import_job_id": job_id,
    }
    async with db_factory() as session:
        assert int(await session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 1
        assert int(await session.scalar(select(func.count(ImportJobAction.id))) or 0) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_retry_import_sync_work_requeues_only_exact_terminal_origin_with_fresh_budget(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    job_id, work_id, _action_id = await _seed_terminal_import_sync_work(
        db_factory,
        tmp_path,
        state=terminal_state,
    )

    async with db_factory() as session:
        job, retrying_count = await sync_queue.retry_import_story_arc_sync_work(
            session,
            job_id,
        )
        await session.commit()

    assert retrying_count == 1
    assert job.status is ImportJobStatus.IMPORTING
    assert job.error_message is None
    assert job.progress_snapshot["status"] == ImportJobStatus.IMPORTING.value
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert job.progress_snapshot["progress"] == 99
    assert job.progress_snapshot["story_arc_placements_queued"] == 1
    assert job.progress_snapshot["story_arc_placements_failed"] == 0
    assert job.progress_snapshot["story_arc_placements_cancelled"] == 0

    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, work_id)
        assert work is not None
        assert work.state is StoryArcSyncWorkState.QUEUED
        assert work.claimable is True
        assert work.attempt_count == 0
        assert work.next_attempt_at is None
        assert work.claim_token is None
        assert work.claimed_at is None
        assert work.cancel_requested_at is None
        assert work.last_error_code is None
        assert work.last_error_category is None
        assert work.last_error_detail is None
        assert work.last_result == {}
        assert await sync_queue._ready_work_ids(
            session,
            now=datetime.now(UTC),
            limit=10,
            import_only=True,
        ) == [work_id]


@pytest.mark.asyncio
async def test_retry_import_sync_work_refuses_inexact_origin_without_mutation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    job_id, work_id, action_id = await _seed_terminal_import_sync_work(
        db_factory,
        tmp_path,
        state=StoryArcSyncWorkState.FAILED,
    )
    async with db_factory() as session:
        action = await session.get(ImportJobAction, action_id)
        assert action is not None
        action.payload = {**dict(action.payload), "source_import_job_id": job_id + 1}
        await session.commit()

    async with db_factory() as session:
        with pytest.raises(ValidationError, match="origin evidence is incomplete"):
            await sync_queue.retry_import_story_arc_sync_work(session, job_id)
        await session.rollback()

    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, work_id)
        job = await session.get(ImportJob, job_id)
        assert work is not None and job is not None
        assert work.state is StoryArcSyncWorkState.FAILED
        assert work.attempt_count == 5
        assert work.last_error_code == "placement_failed"
        assert job.status is ImportJobStatus.STALLED


@pytest.mark.asyncio
async def test_retry_import_sync_work_does_not_mutate_pending_origin_work(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    job_id, work_id, _action_id = await _seed_terminal_import_sync_work(
        db_factory,
        tmp_path,
        state=StoryArcSyncWorkState.FAILED,
    )
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, work_id)
        job = await session.get(ImportJob, job_id)
        assert work is not None and job is not None
        work.state = StoryArcSyncWorkState.RETRY_WAIT
        work.next_attempt_at = retry_at
        job.progress_snapshot = {
            **dict(job.progress_snapshot),
            "story_arc_placements_failed": 0,
            "story_arc_placements_retry_wait": 1,
        }
        await session.commit()

    async with db_factory() as session:
        with pytest.raises(ValidationError, match="No failed or cancelled"):
            await sync_queue.retry_import_story_arc_sync_work(session, job_id)
        await session.rollback()

    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, work_id)
        job = await session.get(ImportJob, job_id)
        assert work is not None and job is not None
        assert work.state is StoryArcSyncWorkState.RETRY_WAIT
        assert work.attempt_count == 5
        assert work.next_attempt_at == retry_at
        assert job.status is ImportJobStatus.STALLED


@pytest.mark.asyncio
async def test_import_enqueue_refuses_to_reuse_work_owned_by_another_staged_entry(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(db_factory, tmp_path)

    async with db_factory() as session:
        job = await session.get(ImportJob, job_id)
        library_file = await session.get(LibraryFile, library_file_id)
        membership = await session.get(IssueStoryArc, membership_id)
        assert job is not None and library_file is not None and membership is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        competing_arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:arc:competing",
            source_ordinal=2,
            name="Competing staged evidence",
            status=ImportedStoryArcStatus.IMPORTED,
            materialized_story_arc_id=story_arc.id,
        )
        competing_entry = ImportedStoryArcEntry(
            imported_story_arc=competing_arc,
            matched_issue_id=membership.issue_id,
            materialized_membership_id=membership.id,
            source_ordinal=1,
            reading_order=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        session.add(competing_entry)
        await session.flush()

        with pytest.raises(
            sync_queue.StoryArcPlacementIntegrationError,
            match="invalid origin binding",
        ) as exc_info:
            await sync_queue.enqueue_import_story_arc_sync_work(
                session,
                job=job,
                library_file=library_file,
                membership=membership,
                story_arc=story_arc,
                imported_story_arc_id=competing_arc.id,
                imported_story_arc_entry_id=competing_entry.id,
                record_action=_record_action,
            )

    assert exc_info.value.code == "import_sync_existing_origin_invalid"


@pytest.mark.asyncio
async def test_import_enqueue_refuses_typed_origin_provenance_drift(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(db_factory, tmp_path)

    async with db_factory() as session:
        job = await session.get(ImportJob, job_id)
        library_file = await session.get(LibraryFile, library_file_id)
        membership = await session.get(IssueStoryArc, membership_id)
        assert job is not None and library_file is not None and membership is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        created = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        assert created.work is not None
        created.work.origin_import_job_id = None
        await session.flush()

        with pytest.raises(
            sync_queue.StoryArcPlacementIntegrationError,
            match="invalid origin binding",
        ) as exc_info:
            await sync_queue.enqueue_import_story_arc_sync_work(
                session,
                job=job,
                library_file=library_file,
                membership=membership,
                story_arc=story_arc,
                imported_story_arc_id=staged_arc_id,
                imported_story_arc_entry_id=entry_id,
                record_action=_record_action,
            )

    assert exc_info.value.code == "import_sync_existing_origin_invalid"


@pytest.mark.asyncio
async def test_import_enqueue_rejects_non_origin_work_without_exact_placement(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(db_factory, tmp_path)
    async with db_factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        library_file = await session.get(LibraryFile, library_file_id)
        assert membership is not None and library_file is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        generation, source_hash = sync_queue._desired_generation(
            library_file,
            membership,
            story_arc,
        )
        session.add(
            StoryArcSyncWork(
                issue_story_arc_id=membership_id,
                library_file_id=library_file_id,
                desired_generation=generation,
                source_signature_hash=source_hash,
                source_file_path=library_file.file_path,
                source_file_size=library_file.file_size,
                source_file_modified_at=library_file.file_modified_at,
                story_arc_revision=story_arc.revision,
                membership_sequence=membership.sequence_number,
                policy_schema_version=story_arc.policy_schema_version or 1,
                state=StoryArcSyncWorkState.QUEUED,
            )
        )
        await session.flush()
        job = await session.get(ImportJob, job_id)
        assert job is not None
        with pytest.raises(sync_queue.StoryArcPlacementIntegrationError) as exc_info:
            await sync_queue.enqueue_import_story_arc_sync_work(
                session,
                job=job,
                library_file=library_file,
                membership=membership,
                story_arc=story_arc,
                imported_story_arc_id=staged_arc_id,
                imported_story_arc_entry_id=entry_id,
                record_action=_record_action,
            )

    assert exc_info.value.code == "import_sync_non_origin_placement_unverified"
    async with db_factory() as session:
        assert int(await session.scalar(select(func.count(ImportJobAction.id))) or 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ownership", "mode", "classification"),
    [
        (
            StoryArcPlacementOwnership.MANAGED,
            StoryArcPlacementMode.COPY,
            "existing_managed_placement",
        ),
        (
            StoryArcPlacementOwnership.REFERENCED,
            StoryArcPlacementMode.REFERENCE_ONLY,
            "existing_referenced_placement",
        ),
    ],
)
async def test_import_enqueue_does_not_steal_existing_placement(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    ownership: StoryArcPlacementOwnership,
    mode: StoryArcPlacementMode,
    classification: str,
) -> None:
    (
        job_id,
        staged_arc_id,
        entry_id,
        membership_id,
        library_file_id,
    ) = await _seed_import_enqueue_context(db_factory, tmp_path)
    async with db_factory() as session:
        placement = StoryArcPlacement(
            issue_story_arc_id=membership_id,
            library_file_id=library_file_id,
            placement_path=str(tmp_path / f"existing-{ownership.value}.cbz"),
            mode=mode,
            ownership=ownership,
            source_kind=StoryArcSourceKind.PULLBOX,
            state=StoryArcPlacementState.CURRENT,
        )
        session.add(placement)
        await session.flush()
        job = await session.get(ImportJob, job_id)
        library_file = await session.get(LibraryFile, library_file_id)
        membership = await session.get(IssueStoryArc, membership_id)
        assert job is not None and library_file is not None and membership is not None
        story_arc = await session.get(StoryArc, membership.story_arc_id)
        assert story_arc is not None
        result = await sync_queue.enqueue_import_story_arc_sync_work(
            session,
            job=job,
            library_file=library_file,
            membership=membership,
            story_arc=story_arc,
            imported_story_arc_id=staged_arc_id,
            imported_story_arc_entry_id=entry_id,
            record_action=_record_action,
        )
        await session.commit()

    assert result.classification == classification
    assert result.work is None and result.action is None
    assert placement.source_import_job_id is None
    assert placement.creating_action_id is None


@pytest.mark.asyncio
async def test_enqueue_adds_every_and_only_eligible_active_membership(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    eligible_id = await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Eligible",
    )
    await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Archived",
        lifecycle=StoryArcLifecycle.ARCHIVED,
    )
    await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Disabled",
        sync_enabled=False,
    )
    await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Unresolved",
        resolution_state=StoryArcResolutionState.AMBIGUOUS,
    )
    await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Unconfigured",
        configured=False,
    )

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        queued = await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()

    async with db_factory() as session:
        rows = list((await session.scalars(select(StoryArcSyncWork))).all())
    assert queued == 1
    assert [row.issue_story_arc_id for row in rows] == [eligible_id]
    assert rows[0].state is StoryArcSyncWorkState.QUEUED


@pytest.mark.asyncio
async def test_enqueue_is_transactional_and_idempotent_for_one_generation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Atomic")

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        assert await enqueue_story_arc_sync_work(session, library_file) == 1
        assert await enqueue_story_arc_sync_work(session, library_file) == 0
        await session.rollback()

    async with db_factory() as session:
        assert await session.scalar(select(func.count(StoryArcSyncWork.id))) == 0


@pytest.mark.asyncio
async def test_discrepancy_skips_an_unchanged_current_generation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Current")
    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()

    async def unexpected_enqueue(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("unchanged current work must be filtered in SQL")

    monkeypatch.setattr(
        "pullbox.services.story_arc_sync_queue._enqueue_pairs",
        unexpected_enqueue,
    )
    async with db_factory() as session:
        assert await discover_story_arc_sync_work(session) == 0


@pytest.mark.asyncio
async def test_enqueue_bounds_memberships_and_discrepancy_recovers_the_remainder(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    first_id = await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="First bounded",
        sequence_number=1,
    )
    second_id = await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Second bounded",
        sequence_number=2,
    )
    monkeypatch.setattr(
        "pullbox.services.story_arc_sync_queue.MAX_STORY_ARC_SYNC_ENQUEUE_MEMBERSHIPS",
        1,
    )

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        assert await enqueue_story_arc_sync_work(session, library_file) == 1
        await session.commit()

    async with db_factory() as session:
        assert await discover_story_arc_sync_work(session) == 1
        await session.commit()

    async with db_factory() as session:
        membership_ids = set(
            (await session.scalars(select(StoryArcSyncWork.issue_story_arc_id))).all()
        )
    assert membership_ids == {first_id, second_id}


@pytest.mark.asyncio
async def test_replaced_source_creates_a_new_desired_generation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Replacement")

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        await enqueue_story_arc_sync_work(session, library_file)
        library_file.source_signature = {"size": 42, "mtime_ns": 2}
        await session.flush()
        await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()

    async with db_factory() as session:
        generations = list(
            (
                await session.scalars(
                    select(StoryArcSyncWork.desired_generation).order_by(StoryArcSyncWork.id.asc())
                )
            ).all()
        )
    assert len(generations) == 2
    assert len(set(generations)) == 2


@pytest.mark.asyncio
async def test_discrepancy_discovery_is_bounded_and_fills_missing_work(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, _library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    first_id = await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="First",
        sequence_number=1,
    )
    await _add_membership(
        db_factory,
        tmp_path,
        issue_id=issue_id,
        name="Second",
        sequence_number=2,
    )

    async with db_factory() as session:
        discovered = await discover_story_arc_sync_work(session, limit=1)
        await session.commit()

    async with db_factory() as session:
        rows = list((await session.scalars(select(StoryArcSyncWork))).all())
    assert discovered == 1
    assert [row.issue_story_arc_id for row in rows] == [first_id]


@pytest.mark.asyncio
async def test_discrepancy_detects_changed_source_after_missed_replacement_enqueue(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Changed Source")
    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        library_file.file_size += 1
        library_file.file_modified_at += timedelta(seconds=1)
        library_file.source_signature = {"size": library_file.file_size, "mtime_ns": 2}
        await session.commit()

    async with db_factory() as session:
        discovered = await discover_story_arc_sync_work(session)
        await session.commit()

    async with db_factory() as session:
        rows = list(
            (
                await session.scalars(select(StoryArcSyncWork).order_by(StoryArcSyncWork.id.asc()))
            ).all()
        )
    assert discovered == 1
    assert len(rows) == 2
    assert rows[0].desired_generation != rows[1].desired_generation


@pytest.mark.asyncio
async def test_discrepancy_detects_source_signature_only_change_after_missed_enqueue(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Signature only")
    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        await enqueue_story_arc_sync_work(session, library_file)
        original_updated_at = library_file.updated_at
        await session.commit()

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        library_file.source_signature = {"size": library_file.file_size, "mtime_ns": 2}
        library_file.updated_at = original_updated_at
        await session.commit()

    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        assert library_file.updated_at == original_updated_at
        existing_work = await session.scalar(select(StoryArcSyncWork))
        assert existing_work is not None
        assert existing_work.source_file_path == library_file.file_path
        assert existing_work.source_file_size == library_file.file_size
        assert existing_work.source_file_modified_at == library_file.file_modified_at
        assert existing_work.source_file_hash == library_file.file_hash
        assert existing_work.source_signature_mtime_ns == 1
        assert library_file.source_signature["mtime_ns"] == 2
        discovered = await discover_story_arc_sync_work(session)
        await session.commit()

    async with db_factory() as session:
        generations = list(
            (await session.scalars(select(StoryArcSyncWork.desired_generation))).all()
        )
    assert discovered == 1
    assert len(set(generations)) == 2


@pytest.mark.asyncio
async def test_claim_has_one_winner_and_stale_running_work_is_recoverable(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    issue_id, library_file_id = await _seed_canonical_file(db_factory, tmp_path)
    await _add_membership(db_factory, tmp_path, issue_id=issue_id, name="Claim")
    async with db_factory() as session:
        library_file = await session.get(LibraryFile, library_file_id)
        assert library_file is not None
        await enqueue_story_arc_sync_work(session, library_file)
        await session.commit()
        work_id = int(await session.scalar(select(StoryArcSyncWork.id)))

    now = datetime.now(UTC)
    async with db_factory() as session:
        first = await claim_story_arc_sync_work(session, work_id, now=now)
    async with db_factory() as session:
        second = await claim_story_arc_sync_work(session, work_id, now=now)
    assert first is not None
    assert second is None

    async with db_factory() as session:
        work = await session.get(StoryArcSyncWork, work_id)
        assert work is not None
        work.claimed_at = now - timedelta(minutes=16)
        await session.commit()
    async with db_factory() as session:
        recovered = await claim_story_arc_sync_work(session, work_id, now=now)
    assert recovered is not None
    assert recovered != first


@pytest.mark.asyncio
async def test_atomic_claim_fences_startup_recovered_origin_until_resume(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    job_id, work_id, _action_id = await _seed_terminal_import_sync_work(
        db_factory,
        tmp_path,
        state=StoryArcSyncWorkState.QUEUED,
    )
    async with db_factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        job.status = ImportJobStatus.PAUSED
        job.progress_snapshot = {
            **dict(job.progress_snapshot or {}),
            "status": ImportJobStatus.PAUSED.value,
            "pause_reason": "startup_recovery",
            "recovered_status": ImportJobStatus.IMPORTING.value,
        }
        await session.commit()

    now = datetime.now(UTC)
    async with db_factory() as session:
        protected_claim = await claim_story_arc_sync_work(
            session,
            work_id,
            now=now,
            import_only=True,
        )
    assert protected_claim is None

    async with db_factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        snapshot = dict(job.progress_snapshot or {})
        snapshot.pop("pause_reason", None)
        snapshot.pop("recovered_status", None)
        snapshot["status"] = ImportJobStatus.IMPORTING.value
        job.status = ImportJobStatus.IMPORTING
        job.progress_snapshot = snapshot
        await session.commit()
    async with db_factory() as session:
        resumed_claim = await claim_story_arc_sync_work(
            session,
            work_id,
            now=now,
            import_only=True,
        )
    assert resumed_claim is not None


@pytest.mark.asyncio
async def test_ready_selection_merges_indexed_lanes_deterministically_and_caps_batch(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=UTC)
    ids = await _seed_ready_selection_work(db_factory, tmp_path, now=now)

    async with db_factory() as session:
        selected = await sync_queue._ready_work_ids(session, now=now, limit=4)

    assert selected == [
        ids["unclaimed_running"],
        ids["queued_older"],
        ids["due_retry"],
        ids["stale_running"],
    ]
    assert len(selected) == len(set(selected)) == 4
    assert ids["queued_newer"] not in selected
    assert ids["future_retry"] not in selected
    assert ids["fresh_running"] not in selected
    assert ids["completed"] not in selected


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_ready_selection_statements_are_bounded_and_index_orderable(dialect: object) -> None:
    statements = sync_queue._ready_work_statements(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC),
        limit=17,
    )

    compiled = [str(statement.compile(dialect=dialect)) for statement in statements]

    assert len(compiled) == 4
    assert all(" LIMIT " in sql.upper() for sql in compiled)
    assert all("COALESCE" not in sql.upper() for sql in compiled)
    assert all(" OR " not in sql.upper() for sql in compiled)
    assert any("next_attempt_at" in sql and "ORDER BY" in sql for sql in compiled)
    assert any("created_at" in sql and "ORDER BY" in sql for sql in compiled)
    assert sum("claimed_at" in sql and "ORDER BY" in sql for sql in compiled) == 2
