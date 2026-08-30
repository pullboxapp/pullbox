"""Task-level contracts for draining durable story-arc synchronization work."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.scheduler import get_registered_tasks
from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncResult,
)
from pullbox.services.story_arc_sync_queue import (
    StoryArcSyncDrainResult,
    claim_story_arc_sync_work,
    enqueue_story_arc_sync_work,
    process_story_arc_sync_work,
)
from pullbox.tasks.story_arc_sync_task import scheduled_sync_story_arc_placements

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


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

    async def fake_process() -> StoryArcSyncDrainResult:
        observed["called"] = True
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
        )

    monkeypatch.setattr(
        "pullbox.tasks.story_arc_sync_task.process_story_arc_sync_work",
        fake_process,
    )

    await scheduled_sync_story_arc_placements()

    assert observed == {"called": True}


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
