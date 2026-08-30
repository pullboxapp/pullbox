"""End-to-end synchronization after a canonical file is acquired later."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.file_ops import register_library_file
from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_sync_queue import process_story_arc_sync_work

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'later-acquisition.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_later_acquisition_registers_once_and_populates_active_arc(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    comics = tmp_path / "comics"
    comics.mkdir()
    arcs = comics / "arcs"
    arcs.mkdir()
    incoming = tmp_path / "incoming" / "Batman 001.cbz"
    incoming.parent.mkdir()
    incoming.write_bytes(b"PK" + b"\x00" * 64)

    async with db_factory() as session:
        session.add(
            SystemConfig(
                key="comics_directory",
                value=str(comics),
                value_type="string",
            )
        )
        root = LibraryRoot(name="Comics", path=str(comics), enabled=True)
        series = Series(title="Batman", sort_title="batman", library_root=root)
        issue = Issue(
            series=series,
            issue_number=1,
            issue_number_text="1",
            status=IssueStatus.WANTED,
        )
        arc = StoryArc(name="Court of Owls", source_kind=StoryArcSourceKind.PULLBOX)
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        session.add_all([root, membership])
        await session.commit()
        issue_id = issue.id
        arc_id = arc.id
        membership_id = membership.id
        root_id = root.id

    async with db_factory() as session:
        await StoryArcPlacementSyncService().update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=StoryArcPlacementPolicyInput(
                mode=StoryArcPlacementPolicyMode.COPY,
                target_library_root_id=root_id,
                destination_root=str(arcs),
                folder_template="{StoryArc}",
                file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
                synchronize=True,
            ),
        )

    async with db_factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue is not None
        canonical = await register_library_file(
            session,
            incoming,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=False,
            library_root_id=root_id,
            transfer_method="copy",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )
        await session.commit()
        canonical_path = canonical.file_path

    result = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=10,
        discover=False,
    )
    repeated = await process_story_arc_sync_work(
        session_factory=db_factory,
        batch_size=10,
        discover=False,
    )

    async with db_factory() as session:
        placement = await session.scalar(
            select(StoryArcPlacement).where(StoryArcPlacement.issue_story_arc_id == membership_id)
        )
        work = await session.scalar(select(StoryArcSyncWork))
        library_file_count = int(
            await session.scalar(
                select(func.count(LibraryFile.id)).where(LibraryFile.issue_id == issue_id)
            )
            or 0
        )
    assert result.completed == 1
    assert result.failed == 0
    assert repeated.claimed == 0
    assert library_file_count == 1
    assert placement is not None
    assert placement.state is StoryArcPlacementState.CURRENT
    assert work is not None
    assert work.state is StoryArcSyncWorkState.COMPLETED
    assert placement.library_file_id == work.library_file_id
    assert canonical_path != placement.placement_path
    assert Path(placement.placement_path).is_relative_to(arcs)
    assert not Path(canonical_path).is_relative_to(arcs)
    assert Path(canonical_path).read_bytes() == incoming.read_bytes()
    assert Path(placement.placement_path).read_bytes() == incoming.read_bytes()
