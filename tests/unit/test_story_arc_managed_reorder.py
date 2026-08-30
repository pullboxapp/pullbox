"""Collision-safe, ownership-aware Story Arc reorder coverage."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
    StoryArcSymlinkStyle,
)
from pullbox.services import story_arc_managed_reorder as reorder_module
from pullbox.services.story_arc_managed_reorder import (
    StoryArcManagedReorderError,
    StoryArcManagedReorderService,
    StoryArcReorderResult,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pytest import MonkeyPatch


@pytest.fixture
async def reorder_db(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reorder.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_reorder_arc(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    mode: StoryArcPlacementPolicyMode = StoryArcPlacementPolicyMode.COPY,
    file_template: str = "{ReadingOrder:03d} - {Series} {IssueNumber}",
    issue_count: int = 3,
) -> tuple[int, tuple[int, ...], tuple[Path, ...], tuple[Path, ...]]:
    library_root_path = tmp_path / "library"
    destination = library_root_path / "StoryArcs"
    library_root_path.mkdir()
    destination.mkdir()
    now = datetime.now(UTC)

    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
        series = Series(title="Batman", sort_title="batman", library_root=root)
        arc = StoryArc(name="Court of Owls", source_kind=StoryArcSourceKind.PULLBOX)
        session.add_all([root, series, arc])
        await session.flush()

        memberships: list[IssueStoryArc] = []
        canonicals: list[Path] = []
        for number in range(1, issue_count + 1):
            canonical = library_root_path / f"Batman {number:03d}.cbz"
            canonical.write_bytes(f"canonical-{number}".encode())
            issue = Issue(
                series=series,
                issue_number=float(number),
                issue_number_text=str(number),
                title=f"Part {number}",
            )
            library_file = LibraryFile(
                issue=issue,
                library_root=root,
                file_path=str(canonical),
                file_name=canonical.name,
                file_size=canonical.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=now,
                match_confidence=MatchConfidence.HIGH,
            )
            membership = IssueStoryArc(
                story_arc=arc,
                issue=issue,
                sequence_number=number,
                source_ordinal=number,
                resolution_state=StoryArcResolutionState.RESOLVED,
                source_kind=StoryArcSourceKind.PULLBOX,
                source_issue_number_text=str(number),
            )
            session.add_all([library_file, membership])
            memberships.append(membership)
            canonicals.append(canonical)
        await session.commit()
        arc_id = arc.id
        membership_ids = tuple(item.id for item in memberships)
        root_id = root.id
        revision = arc.revision

    placement_service = StoryArcPlacementSyncService()
    async with factory() as session:
        await placement_service.update_policy(
            session,
            arc_id,
            expected_revision=revision,
            proposal=StoryArcPlacementPolicyInput(
                mode=mode,
                target_library_root_id=root_id,
                destination_root=str(destination),
                folder_template="{StoryArc}",
                file_template=file_template,
                symlink_style=(
                    StoryArcSymlinkStyle.RELATIVE
                    if mode is StoryArcPlacementPolicyMode.SYMLINK
                    else None
                ),
                synchronize=True,
            ),
        )
    targets: list[Path] = []
    for membership_id in membership_ids:
        async with factory() as session:
            result = await placement_service.sync_membership(session, arc_id, membership_id)
            assert result.placement is not None
            targets.append(Path(result.placement.placement_path))
    return (
        arc_id,
        membership_ids,
        tuple(canonicals),
        tuple(targets),
    )


def test_pending_coordinator_query_compiles_for_sqlite_and_postgresql() -> None:
    statement = reorder_module._pending_coordinator_statement(7)

    sqlite_sql = str(statement.compile(dialect=sqlite.dialect()))
    postgres_sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "JSON_EXTRACT" in sqlite_sql
    assert "story_arc_placements.last_result" in sqlite_sql
    assert "story_arc_placements.last_result ->>" in postgres_sql
    assert "LIMIT" in sqlite_sql.upper()
    assert "LIMIT" in postgres_sql.upper()


async def test_preview_then_confirm_excludes_unrelated_reference_from_result(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    first_id, second_id, third_id = membership_ids
    first_target, second_target, third_target = original_targets
    third_bytes = third_target.read_bytes()

    async with reorder_db() as session:
        third_placement = await session.scalar(
            select(StoryArcPlacement).where(StoryArcPlacement.issue_story_arc_id == third_id)
        )
        assert third_placement is not None
        third_placement.mode = StoryArcPlacementMode.REFERENCE_ONLY
        third_placement.ownership = StoryArcPlacementOwnership.REFERENCED
        third_placement.symlink_style = None
        third_placement.creating_action_id = None
        await session.commit()
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        expected_revision = arc.revision

    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            first_id,
            direction="down",
            expected_revision=expected_revision,
        )

    assert preview.requires_confirmation is True
    assert preview.filesystem_mutated is False
    assert preview.managed_rename_count == 2
    assert preview.referenced_drift_count == 0
    assert preview.referenced_preserved_count == 0
    assert {item.membership_id for item in preview.items if item.action == "rename"} == {
        first_id,
        second_id,
    }
    renamed_by_membership = {
        item.membership_id: item for item in preview.items if item.action == "rename"
    }
    first_new_target = Path(str(renamed_by_membership[first_id].new_path))
    second_new_target = Path(str(renamed_by_membership[second_id].new_path))
    assert first_target.read_bytes() == b"canonical-1"
    assert second_target.read_bytes() == b"canonical-2"

    async with reorder_db() as session:
        result = await service.confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )

    assert result.revision == expected_revision + 1
    assert result.managed_renamed == 2
    assert result.referenced_preserved == 0
    assert first_new_target.read_bytes() == b"canonical-1"
    assert second_new_target.read_bytes() == b"canonical-2"
    assert not first_target.exists()
    assert not second_target.exists()
    assert third_target.read_bytes() == third_bytes
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))

    async with reorder_db() as session:
        memberships = list(
            (
                await session.scalars(
                    select(IssueStoryArc)
                    .where(IssueStoryArc.story_arc_id == arc_id)
                    .order_by(
                        IssueStoryArc.sequence_number,
                        IssueStoryArc.source_ordinal,
                        IssueStoryArc.id,
                    )
                )
            ).all()
        )
        assert [item.id for item in memberships] == [second_id, first_id, third_id]
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
        by_membership = {item.issue_story_arc_id: item for item in placements}
        assert by_membership[first_id].placement_path == str(first_new_target)
        assert by_membership[second_id].placement_path == str(second_new_target)
        assert by_membership[third_id].placement_path == str(third_target)
        assert by_membership[third_id].state is StoryArcPlacementState.CURRENT


async def test_direct_path_swap_stages_both_artifacts_before_collision_safe_publish(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db,
        tmp_path,
        file_template="{ReadingOrder:03d}",
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    renamed = {item.membership_id: item for item in preview.items if item.action == "rename"}
    assert renamed[membership_ids[0]].new_path == str(original_targets[1])
    assert renamed[membership_ids[1]].new_path == str(original_targets[0])

    async with reorder_db() as session:
        await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-2",
        b"canonical-1",
    ]
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))


@pytest.mark.parametrize(
    "mode",
    [StoryArcPlacementPolicyMode.HARDLINK, StoryArcPlacementPolicyMode.SYMLINK],
)
async def test_reorder_preserves_managed_link_representation(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: StoryArcPlacementPolicyMode,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db,
        tmp_path,
        mode=mode,
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    new_by_membership = {
        item.membership_id: Path(str(item.new_path))
        for item in preview.items
        if item.action == "rename"
    }
    async with reorder_db() as session:
        await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )

    for membership_id, canonical in zip(membership_ids[:2], canonicals[:2], strict=True):
        target = new_by_membership[membership_id]
        if mode is StoryArcPlacementPolicyMode.HARDLINK:
            assert target.stat().st_ino == canonical.stat().st_ino
        else:
            assert target.is_symlink()
            assert target.resolve(strict=True) == canonical.resolve(strict=True)
    assert all(not path.exists() and not path.is_symlink() for path in original_targets[:2])
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]


async def test_changed_managed_artifact_blocks_confirm_before_order_or_path_update(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        revision = arc.revision
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=revision,
        )

    original_targets[0].write_bytes(b"user changed this placement")
    with pytest.raises(StoryArcManagedReorderError) as blocked:
        async with reorder_db() as session:
            await StoryArcManagedReorderService().confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert blocked.value.code == "managed_placement_changed"
    assert canonicals[0].read_bytes() == b"canonical-1"

    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None and arc.revision == revision
        memberships = list(
            (
                await session.scalars(
                    select(IssueStoryArc)
                    .where(IssueStoryArc.story_arc_id == arc_id)
                    .order_by(IssueStoryArc.sequence_number)
                )
            ).all()
        )
        assert [item.id for item in memberships] == list(membership_ids)
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
        assert {item.placement_path for item in placements} == {
            str(path) for path in original_targets
        }


async def test_cancelled_reorder_restores_old_paths_and_records_retryable_truth(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    checks = 0

    def cancel_after_staging() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(StoryArcManagedReorderError) as cancelled:
        async with reorder_db() as session:
            await StoryArcManagedReorderService().confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
                cancellation_requested=cancel_after_staging,
            )
    assert cancelled.value.code == "reorder_cancelled"
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-1",
        b"canonical-2",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))

    async with reorder_db() as session:
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
        assert all(item.operation_token is None for item in placements)
        assert all(item.last_result["status"] == "rename_cancelled" for item in placements[:2])
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        retry_preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    async with reorder_db() as session:
        retry_result = await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=retry_preview.story_arc_id,
            membership_id=retry_preview.membership_id,
            direction=retry_preview.direction,
            expected_revision=retry_preview.expected_revision,
            preview_token=retry_preview.preview_token,
        )
    assert retry_result.revision == retry_preview.expected_revision + 1


async def test_untracked_rendered_destination_blocks_preview_without_any_mutation(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    blocked_target = original_targets[0].with_name("002 - Batman 1.cbz")
    blocked_target.write_bytes(b"user-owned collision")
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        revision = arc.revision
        with pytest.raises(StoryArcManagedReorderError) as collision:
            await StoryArcManagedReorderService().preview_adjacent_move(
                session,
                arc_id,
                membership_ids[0],
                direction="down",
                expected_revision=revision,
            )
    assert collision.value.code == "destination_collision"
    assert blocked_target.read_bytes() == b"user-owned collision"
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-1",
        b"canonical-2",
    ]
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None and arc.revision == revision
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
        assert all(item.operation_token is None for item in placements)


async def test_destination_race_never_deletes_foreign_entry(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    first_rename = next(item for item in preview.items if item.action == "rename")
    foreign_path = Path(str(first_rename.temporary_path))
    original_link = os.link

    def race_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"foreign race winner")
        finally:
            os.close(descriptor)
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(reorder_module.os, "link", race_link)
    with pytest.raises(StoryArcManagedReorderError) as failed:
        async with reorder_db() as session:
            await StoryArcManagedReorderService().confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert failed.value.code == "reorder_failed"
    assert foreign_path.read_bytes() == b"foreign race winner"
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-1",
        b"canonical-2",
    ]


async def test_failure_after_source_unlink_retains_destination_for_same_token_restart(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    first_rename = next(item for item in preview.items if item.action == "rename")
    temporary_path = Path(str(first_rename.temporary_path))
    original_fsync = reorder_module._fsync_directory
    fsync_calls = 0

    def interrupt_after_source_unlink(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise RuntimeError("simulated interruption after source unlink")
        original_fsync(descriptor)

    monkeypatch.setattr(
        reorder_module,
        "_fsync_directory",
        interrupt_after_source_unlink,
    )
    with pytest.raises(RuntimeError, match="after source unlink"):
        async with reorder_db() as session:
            await service.confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert temporary_path.read_bytes() == b"canonical-1"
    assert not original_targets[0].exists()
    async with reorder_db() as session:
        placement = await session.scalar(
            select(StoryArcPlacement).where(
                StoryArcPlacement.issue_story_arc_id == membership_ids[0]
            )
        )
        assert placement is not None
        assert placement.placement_path == str(original_targets[0])
        assert placement.operation_token is not None
        assert placement.last_result["status"] == "rename_prepared"

    monkeypatch.setattr(reorder_module, "_fsync_directory", original_fsync)
    async with reorder_db() as session:
        result = await service.confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.revision == preview.expected_revision + 1
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))


async def test_prepared_journal_commits_before_filesystem_and_db_paths_stay_old_on_failure(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    observed_committed_checkpoint = False

    def fail_first_move(**_kwargs: object) -> None:
        nonlocal observed_committed_checkpoint
        with sqlite3.connect(tmp_path / "reorder.db") as connection:
            rows = connection.execute(
                "SELECT placement_path, operation_token, last_result "
                "FROM story_arc_placements WHERE issue_story_arc_id IN (?, ?) "
                "ORDER BY issue_story_arc_id",
                membership_ids[:2],
            ).fetchall()
        assert [row[0] for row in rows] == [str(path) for path in original_targets[:2]]
        assert all(row[1] for row in rows)
        assert all(json.loads(row[2])["status"] == "rename_prepared" for row in rows)
        observed_committed_checkpoint = True
        raise OSError("simulated publish failure")

    monkeypatch.setattr(reorder_module, "_exclusive_move", fail_first_move)
    with pytest.raises(StoryArcManagedReorderError) as failed:
        async with reorder_db() as session:
            await StoryArcManagedReorderService().confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert failed.value.code == "reorder_failed"
    assert observed_committed_checkpoint is True

    async with reorder_db() as session:
        rows = list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .where(StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2]))
                    .order_by(StoryArcPlacement.issue_story_arc_id)
                )
            ).all()
        )
        assert [row.placement_path for row in rows] == [str(path) for path in original_targets[:2]]
        assert all(row.operation_token is None for row in rows)
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None and arc.revision == preview.expected_revision


async def test_same_preview_token_reconciles_restart_after_filesystem_publish(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    renamed = [item for item in preview.items if item.action == "rename"]
    new_paths = {item.placement_id: Path(str(item.new_path)) for item in renamed}

    original_reconcile = StoryArcManagedReorderService._reconcile_success

    async def crash_before_db_reconcile(
        _self: StoryArcManagedReorderService,
        _session: AsyncSession,
        _plan: object,
        _filesystem_result: object,
    ) -> int:
        raise RuntimeError("simulated process interruption before DB reconcile")

    monkeypatch.setattr(
        StoryArcManagedReorderService,
        "_reconcile_success",
        crash_before_db_reconcile,
    )
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        async with reorder_db() as session:
            await service.confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )

    assert all(path.exists() for path in new_paths.values())
    assert all(not path.exists() for path in original_targets[:2])
    async with reorder_db() as session:
        prepared = list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .where(StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2]))
                    .order_by(StoryArcPlacement.issue_story_arc_id)
                )
            ).all()
        )
        assert [row.placement_path for row in prepared] == [
            str(path) for path in original_targets[:2]
        ]
        assert all(row.operation_token for row in prepared)
        assert all(row.last_result["status"] == "rename_prepared" for row in prepared)

    monkeypatch.setattr(
        StoryArcManagedReorderService,
        "_reconcile_success",
        original_reconcile,
    )
    async with reorder_db() as session:
        result: StoryArcReorderResult = await service.confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.revision == preview.expected_revision + 1

    async with reorder_db() as session:
        reconciled = list(
            (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2])
                    )
                )
            ).all()
        )
        assert {row.placement_path for row in reconciled} == {
            str(path) for path in new_paths.values()
        }
        assert all(row.operation_token is None for row in reconciled)


async def test_fresh_service_discovers_and_finishes_prepared_journal_without_browser_token(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    first_process = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        original_preview = await first_process.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
        plan = first_process._decode_plan(original_preview.preview_token)
        await first_process._verify_and_prepare(session, plan)

    # Simulate a new process and browser navigation with no access to the
    # original request's confirmation token.
    fresh_process = StoryArcManagedReorderService()
    async with reorder_db() as session:
        recovered = await fresh_process.load_pending_preview(session, arc_id)
    assert recovered is not None
    assert recovered.recovery_pending is True
    assert recovered.story_arc_id == arc_id
    assert recovered.membership_id == membership_ids[0]
    assert recovered.managed_rename_count == 2
    assert recovered.preview_token

    async with reorder_db() as session:
        result = await fresh_process.confirm_adjacent_move(
            session,
            story_arc_id=recovered.story_arc_id,
            membership_id=recovered.membership_id,
            direction=recovered.direction,
            expected_revision=recovered.expected_revision,
            preview_token=recovered.preview_token,
        )
    assert result.revision == recovered.expected_revision + 1
    assert all(not path.exists() for path in original_targets[:2])
    async with reorder_db() as session:
        assert await fresh_process.load_pending_preview(session, arc_id) is None
        placements = list(
            (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2])
                    )
                )
            ).all()
        )
        assert all(row.operation_token is None for row in placements)


async def test_multiple_prepared_coordinators_are_discovered_and_retired_sequentially(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, _canonicals, _original_targets = await _seed_reorder_arc(
        reorder_db,
        tmp_path,
        issue_count=4,
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        first_preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
        second_preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[2],
            direction="down",
            expected_revision=arc.revision,
        )
    async with reorder_db() as session:
        await service._verify_and_prepare(
            session,
            service._decode_plan(first_preview.preview_token),
        )
    async with reorder_db() as session:
        await service._verify_and_prepare(
            session,
            service._decode_plan(second_preview.preview_token),
        )

    fresh_service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        first_recovered = await fresh_service.load_pending_preview(session, arc_id)
    assert first_recovered is not None
    assert first_recovered.membership_id == membership_ids[0]

    async with reorder_db() as session:
        await fresh_service.confirm_adjacent_move(
            session,
            story_arc_id=first_recovered.story_arc_id,
            membership_id=first_recovered.membership_id,
            direction=first_recovered.direction,
            expected_revision=first_recovered.expected_revision,
            preview_token=first_recovered.preview_token,
        )

    async with reorder_db() as session:
        second_recovered = await fresh_service.load_pending_preview(session, arc_id)
    assert second_recovered is not None
    assert second_recovered.membership_id == membership_ids[2]

    # The first completed move advanced the arc revision.  Confirming the next
    # durable coordinator therefore performs a safe old-path restore and
    # retires its stale journal rather than hiding it forever.
    with pytest.raises(StoryArcManagedReorderError) as stale:
        async with reorder_db() as session:
            await fresh_service.confirm_adjacent_move(
                session,
                story_arc_id=second_recovered.story_arc_id,
                membership_id=second_recovered.membership_id,
                direction=second_recovered.direction,
                expected_revision=second_recovered.expected_revision,
                preview_token=second_recovered.preview_token,
            )
    assert stale.value.code == "revision_conflict"
    async with reorder_db() as session:
        assert await fresh_service.load_pending_preview(session, arc_id) is None


async def test_revision_bump_after_filesystem_publish_restores_paths_and_retires_journal(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    original_reconcile = StoryArcManagedReorderService._reconcile_success
    revision_bumped = False

    async def bump_revision_then_reconcile(
        inner_service: StoryArcManagedReorderService,
        session: AsyncSession,
        plan: object,
        filesystem_result: object,
    ) -> int:
        nonlocal revision_bumped
        if not revision_bumped:
            async with reorder_db() as concurrent_session:
                concurrent_arc = await concurrent_session.get(StoryArc, arc_id)
                assert concurrent_arc is not None
                concurrent_arc.revision += 1
                await concurrent_session.commit()
            revision_bumped = True
        return await original_reconcile(
            inner_service,
            session,
            plan,  # type: ignore[arg-type]
            filesystem_result,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        StoryArcManagedReorderService,
        "_reconcile_success",
        bump_revision_then_reconcile,
    )
    with pytest.raises(StoryArcManagedReorderError) as conflicted:
        async with reorder_db() as session:
            await service.confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert conflicted.value.code == "revision_conflict"
    assert conflicted.value.category == "conflict"
    assert revision_bumped is True
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-1",
        b"canonical-2",
    ]
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))

    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None and arc.revision == preview.expected_revision + 1
        memberships = list(
            (
                await session.scalars(
                    select(IssueStoryArc)
                    .where(IssueStoryArc.id.in_(membership_ids[:2]))
                    .order_by(IssueStoryArc.id)
                )
            ).all()
        )
        assert [row.sequence_number for row in memberships] == [1, 2]
        placements = list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .where(StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2]))
                    .order_by(StoryArcPlacement.issue_story_arc_id)
                )
            ).all()
        )
        assert [row.placement_path for row in placements] == [
            str(path) for path in original_targets[:2]
        ]
        assert all(row.operation_token is None for row in placements)
        assert all(row.last_result["status"] == "rename_failed" for row in placements)


async def test_failed_restore_after_reconcile_conflict_keeps_discoverable_recovery(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    async def reject_reconciliation(
        _service: StoryArcManagedReorderService,
        _session: AsyncSession,
        _plan: object,
        _filesystem_result: object,
    ) -> int:
        raise StoryArcManagedReorderError(
            "revision_conflict",
            "simulated revision race",
            category="conflict",
        )

    monkeypatch.setattr(
        StoryArcManagedReorderService,
        "_reconcile_success",
        reject_reconciliation,
    )
    monkeypatch.setattr(reorder_module, "_restore_old_paths", lambda _plan: False)

    with pytest.raises(StoryArcManagedReorderError) as recovery:
        async with reorder_db() as session:
            await service.confirm_adjacent_move(
                session,
                story_arc_id=preview.story_arc_id,
                membership_id=preview.membership_id,
                direction=preview.direction,
                expected_revision=preview.expected_revision,
                preview_token=preview.preview_token,
            )
    assert recovery.value.code == "reorder_recovery_required"
    assert recovery.value.category == "recovery"
    assert all(not path.exists() for path in original_targets[:2])

    fresh_service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        recovered = await fresh_service.load_pending_preview(session, arc_id)
        placements = list(
            (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.issue_story_arc_id.in_(membership_ids[:2])
                    )
                )
            ).all()
        )
    assert recovered is not None and recovered.recovery_pending is True
    assert recovered.preview_token
    assert all(row.operation_token is not None for row in placements)
    assert all(row.last_result["status"] == "rename_recovery_required" for row in placements)


async def test_same_token_finishes_interrupted_exclusive_link_publish_window(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    service = StoryArcManagedReorderService()
    async with reorder_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await service.preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
        plan = service._decode_plan(preview.preview_token)
        await service._verify_and_prepare(session, plan)

    first_plan = next(item for item in plan.placements if item.action == "rename")
    assert first_plan.temporary_path is not None
    os.link(
        first_plan.old_path,
        first_plan.temporary_path,
        follow_symlinks=False,
    )
    assert Path(first_plan.old_path).exists()
    assert Path(first_plan.temporary_path).exists()

    async with reorder_db() as session:
        result = await service.confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.revision == preview.expected_revision + 1
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    assert not list((tmp_path / "arcs").rglob(".pullbox-story-arc-reorder-*"))
    assert all(not path.exists() for path in original_targets[:2])


async def test_impacted_referenced_placement_is_never_renamed(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    referenced_target = original_targets[1]
    referenced_bytes = referenced_target.read_bytes()
    async with reorder_db() as session:
        placement = await session.scalar(
            select(StoryArcPlacement).where(
                StoryArcPlacement.issue_story_arc_id == membership_ids[1]
            )
        )
        assert placement is not None
        placement.mode = StoryArcPlacementMode.REFERENCE_ONLY
        placement.ownership = StoryArcPlacementOwnership.REFERENCED
        placement.symlink_style = None
        await session.commit()
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    assert preview.managed_rename_count == 1
    assert preview.referenced_drift_count == 1
    referenced_preview = next(item for item in preview.items if item.ownership == "referenced")
    assert referenced_preview.old_path == str(referenced_target)
    assert referenced_preview.new_path == str(referenced_target)

    async with reorder_db() as session:
        result = await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.referenced_preserved == 1
    assert referenced_target.read_bytes() == referenced_bytes
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    async with reorder_db() as session:
        placement = await session.scalar(
            select(StoryArcPlacement).where(
                StoryArcPlacement.issue_story_arc_id == membership_ids[1]
            )
        )
        assert placement is not None
        assert placement.placement_path == str(referenced_target)
        assert placement.state is StoryArcPlacementState.DRIFTED
        assert placement.last_result["artifact_mutated"] is False


async def test_concurrent_nonrenamed_inspection_truth_is_not_overwritten_at_reconcile(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, _canonicals, original_targets = await _seed_reorder_arc(
        reorder_db, tmp_path
    )
    async with reorder_db() as session:
        referenced = await session.scalar(
            select(StoryArcPlacement).where(
                StoryArcPlacement.issue_story_arc_id == membership_ids[1]
            )
        )
        assert referenced is not None
        referenced.mode = StoryArcPlacementMode.REFERENCE_ONLY
        referenced.ownership = StoryArcPlacementOwnership.REFERENCED
        referenced.symlink_style = None
        referenced_id = referenced.id
        target_fingerprint = dict(referenced.last_result["target_fingerprint"])
        await session.commit()
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )

    concurrent_result = {
        "schema_version": 1,
        "status": "concurrent_missing_inspection",
        "target_fingerprint": target_fingerprint,
    }
    original_complete = reorder_module._complete_filesystem_plan

    def complete_then_record_concurrent_reference_truth(
        plan: object,
        cancellation_requested: object,
    ) -> object:
        result = original_complete(plan, cancellation_requested)  # type: ignore[arg-type]
        with sqlite3.connect(tmp_path / "reorder.db") as connection:
            connection.execute(
                "UPDATE story_arc_placements SET state = ?, last_result = ? WHERE id = ?",
                ("missing", json.dumps(concurrent_result), referenced_id),
            )
            connection.commit()
        return result

    monkeypatch.setattr(
        reorder_module,
        "_complete_filesystem_plan",
        complete_then_record_concurrent_reference_truth,
    )
    async with reorder_db() as session:
        result = await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.revision == preview.expected_revision + 1
    assert result.referenced_preserved == 1
    assert original_targets[1].read_bytes() == b"canonical-2"
    async with reorder_db() as session:
        referenced = await session.get(StoryArcPlacement, referenced_id)
        assert referenced is not None
        assert referenced.state is StoryArcPlacementState.MISSING
        assert referenced.last_result == concurrent_result
        assert referenced.placement_path == str(original_targets[1])
        assert referenced.rendered_reading_order == 2


async def test_concurrent_managed_unchanged_truth_is_not_overwritten_at_reconcile(
    reorder_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    arc_id, membership_ids, canonicals, original_targets = await _seed_reorder_arc(
        reorder_db,
        tmp_path,
        file_template="{Series} {IssueNumber}",
    )
    async with reorder_db() as session:
        victim = await session.scalar(
            select(StoryArcPlacement).where(
                StoryArcPlacement.issue_story_arc_id == membership_ids[0]
            )
        )
        assert victim is not None
        victim_id = victim.id
        target_fingerprint = dict(victim.last_result["target_fingerprint"])
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None
        preview = await StoryArcManagedReorderService().preview_adjacent_move(
            session,
            arc_id,
            membership_ids[0],
            direction="down",
            expected_revision=arc.revision,
        )
    assert preview.managed_rename_count == 0
    assert all(item.action == "managed_unchanged" for item in preview.items)

    concurrent_result = {
        "schema_version": 1,
        "status": "concurrent_drift_inspection",
        "target_fingerprint": target_fingerprint,
    }
    original_complete = reorder_module._complete_filesystem_plan

    def complete_then_record_concurrent_managed_truth(
        plan: object,
        cancellation_requested: object,
    ) -> object:
        result = original_complete(plan, cancellation_requested)  # type: ignore[arg-type]
        with sqlite3.connect(tmp_path / "reorder.db") as connection:
            connection.execute(
                "UPDATE story_arc_placements SET state = ?, last_result = ? WHERE id = ?",
                ("drifted", json.dumps(concurrent_result), victim_id),
            )
            connection.commit()
        return result

    monkeypatch.setattr(
        reorder_module,
        "_complete_filesystem_plan",
        complete_then_record_concurrent_managed_truth,
    )
    async with reorder_db() as session:
        result = await StoryArcManagedReorderService().confirm_adjacent_move(
            session,
            story_arc_id=preview.story_arc_id,
            membership_id=preview.membership_id,
            direction=preview.direction,
            expected_revision=preview.expected_revision,
            preview_token=preview.preview_token,
        )
    assert result.revision == preview.expected_revision + 1
    assert [path.read_bytes() for path in original_targets[:2]] == [
        b"canonical-1",
        b"canonical-2",
    ]
    assert [path.read_bytes() for path in canonicals] == [
        b"canonical-1",
        b"canonical-2",
        b"canonical-3",
    ]
    async with reorder_db() as session:
        victim = await session.get(StoryArcPlacement, victim_id)
        assert victim is not None
        assert victim.state is StoryArcPlacementState.DRIFTED
        assert victim.last_result == concurrent_result
        assert victim.rendered_reading_order == 1
