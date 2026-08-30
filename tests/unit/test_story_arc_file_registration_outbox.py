"""Canonical registration contracts for story-arc sync outbox creation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

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
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_registration_target(
    session: AsyncSession,
    tmp_path: Path,
    *,
    with_arc: bool,
) -> tuple[Issue, LibraryRoot, Path]:
    comics = tmp_path / "comics"
    comics.mkdir(exist_ok=True)
    arcs = tmp_path / "arcs"
    arcs.mkdir(exist_ok=True)
    incoming = tmp_path / "incoming" / "Batman 001.cbz"
    incoming.parent.mkdir(exist_ok=True)
    incoming.write_bytes(b"PK" + b"\x00" * 64)
    session.add(
        SystemConfig(
            key="comics_directory",
            value=str(comics),
            value_type="string",
        )
    )
    root = LibraryRoot(name="Comics", path=str(comics), enabled=True)
    series = Series(title="Batman", sort_title="batman", library_root=root)
    issue = Issue(series=series, issue_number=1, issue_number_text="1", status=IssueStatus.WANTED)
    session.add(issue)
    await session.flush()
    if with_arc:
        arc = StoryArc(
            name="Court of Owls",
            source_kind=StoryArcSourceKind.PULLBOX,
            sync_enabled=True,
            target_library_root_id=root.id,
            policy_schema_version=1,
            policy_snapshot={
                "schema_version": 1,
                "mode": "copy",
                "target_library_root_id": root.id,
                "destination_root": str(arcs),
                "folder_template": "{StoryArc}",
                "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
                "symlink_style": None,
                "synchronize": True,
            },
            revision=2,
        )
        session.add(
            IssueStoryArc(
                story_arc=arc,
                issue=issue,
                sequence_number=1,
                source_ordinal=1,
                resolution_state=StoryArcResolutionState.RESOLVED,
                source_kind=StoryArcSourceKind.PULLBOX,
                sync_eligible=True,
            )
        )
        await session.flush()
    return issue, root, incoming


@pytest.mark.asyncio
async def test_registration_commits_canonical_file_and_sync_work_together(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with db_factory() as session:
        issue, root, incoming = await _seed_registration_target(
            session,
            tmp_path,
            with_arc=True,
        )
        library_file = await register_library_file(
            session,
            incoming,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=False,
            library_root_id=root.id,
            transfer_method="copy",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )
        await session.commit()
        library_file_id = library_file.id

    async with db_factory() as session:
        assert await session.get(LibraryFile, library_file_id) is not None
        work = await session.scalar(select(StoryArcSyncWork))
        assert work is not None
        assert work.library_file_id == library_file_id


@pytest.mark.asyncio
async def test_outbox_enqueue_failure_never_fails_canonical_registration(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    monkeypatch.setattr(
        "pullbox.services.story_arc_sync_queue.enqueue_story_arc_sync_work",
        enqueue,
    )
    async with db_factory() as session:
        issue, root, incoming = await _seed_registration_target(
            session,
            tmp_path,
            with_arc=False,
        )
        library_file = await register_library_file(
            session,
            incoming,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=False,
            library_root_id=root.id,
            transfer_method="copy",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )
        await session.commit()
        library_file_id = library_file.id

    async with db_factory() as session:
        assert await session.get(LibraryFile, library_file_id) is not None
        assert await session.scalar(select(func.count(StoryArcSyncWork.id))) == 0
    enqueue.assert_awaited_once()
