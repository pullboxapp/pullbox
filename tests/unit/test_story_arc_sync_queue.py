"""Durable outbox contracts for automatic story-arc synchronization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
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


def _policy_snapshot(root_id: int, destination: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "copy",
        "target_library_root_id": root_id,
        "destination_root": str(destination),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
        "symlink_style": None,
        "synchronize": True,
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
) -> int:
    destination = tmp_path / f"arcs-{name}"
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
            policy_snapshot=(_policy_snapshot(root_id, destination) if configured else {}),
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
