"""Signed, bounded preparation for Story Arc placement-policy migration."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.import_job import ImportJobActionStatus, ImportJobStatus
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
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.schemas.story_arc_placement import StoryArcPolicyMigrationConfirmationRequest
from pullbox.services import story_arc_policy_migration as policy_migration_module
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_policy_migration import (
    STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
    StoryArcPolicyMigrationError,
    StoryArcPolicyMigrationService,
    _active_operation_statement,
    _membership_scope_statement,
    _terminal_sync_recovery_pending,
    _terminal_sync_work_statement,
    _TerminalSyncWorkScope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def migration_db(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy-migration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _policy(root_id: int, destination: Path) -> StoryArcPlacementPolicyInput:
    return StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.COPY,
        target_library_root_id=root_id,
        destination_root=str(destination),
        folder_template="{StoryArc}",
        file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
        synchronize=True,
    )


async def _seed_migration_scope(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int, Path, Path, tuple[Path, ...]]:
    library = tmp_path / "library"
    current_root = tmp_path / "current-arcs"
    proposed_root = tmp_path / "proposed-arcs"
    library.mkdir()
    current_root.mkdir()
    proposed_root.mkdir()
    canonical_paths = tuple(library / f"Issue-{number}.cbz" for number in range(1, 4))
    for number, path in enumerate(canonical_paths, start=1):
        path.write_bytes(f"canonical-{number}".encode())

    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(library), enabled=True)
        series = Series(
            title="Migration Series",
            sort_title="migration series",
            year_start=2026,
            library_root=root,
        )
        arc = StoryArc(name="Migration Arc", source_kind=StoryArcSourceKind.PULLBOX)
        memberships: list[IssueStoryArc] = []
        for number, canonical in enumerate(canonical_paths, start=1):
            issue = Issue(
                series=series,
                issue_number=float(number),
                issue_number_text=str(number),
                title=f"Part {number}",
            )
            library_file = LibraryFile(
                file_path=str(canonical),
                file_name=canonical.name,
                file_size=canonical.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue=issue,
                library_root=root,
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
            memberships.append(membership)
            session.add_all([library_file, membership])
        await session.commit()
        arc_id = arc.id
        root_id = root.id
        membership_ids = tuple(item.id for item in memberships)

    placement_service = StoryArcPlacementSyncService()
    async with factory() as session:
        await placement_service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_policy(root_id, current_root),
        )
    for membership_id in membership_ids[:2]:
        async with factory() as session:
            await placement_service.sync_membership(session, arc_id, membership_id)

    referenced_path = tmp_path / "legacy" / "user-owned.cbz"
    referenced_path.parent.mkdir()
    referenced_path.write_bytes(canonical_paths[2].read_bytes())
    async with factory() as session:
        third_file = await session.scalar(
            select(LibraryFile).where(LibraryFile.file_path == str(canonical_paths[2])).limit(1)
        )
        assert third_file is not None
        session.add(
            StoryArcPlacement(
                issue_story_arc_id=membership_ids[2],
                library_file_id=third_file.id,
                library_root_id=root_id,
                placement_path=str(referenced_path),
                mode=StoryArcPlacementMode.REFERENCE_ONLY,
                ownership=StoryArcPlacementOwnership.REFERENCED,
                source_kind=StoryArcSourceKind.FOLDER,
                rendered_reading_order=3,
                policy_schema_version=1,
                source_fingerprint={
                    "sha256": hashlib.sha256(canonical_paths[2].read_bytes()).hexdigest()
                },
                state=StoryArcPlacementState.CURRENT,
                last_result={"schema_version": 1, "status": "complete"},
            )
        )
        await session.commit()
    return arc_id, root_id, current_root, proposed_root, canonical_paths


async def test_preview_is_complete_but_returns_one_bounded_keyset_page_without_mutation(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, current_root, proposed_root, canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    service = StoryArcPolicyMigrationService()

    async with migration_db() as session:
        preview = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=_policy(root_id, proposed_root),
            limit=1,
            after_placement_id=0,
        )

    assert preview.current_policy.destination_root == str(current_root.resolve())
    assert preview.proposed_policy.destination_root == str(proposed_root.resolve())
    assert preview.expected_revision == 2
    assert preview.total_placement_count == 3
    assert preview.managed_migrate_count == 2
    assert preview.managed_remove_count == 0
    assert preview.managed_unchanged_count == 0
    assert preview.referenced_preserved_count == 1
    assert preview.collision_count == 0
    assert preview.blocked_count == 0
    assert preview.required_bytes == sum(path.stat().st_size for path in canonicals[:2])
    assert len(preview.scope_digest) == 64
    assert preview.required_confirmation == STORY_ARC_POLICY_MIGRATION_CONFIRMATION
    assert preview.requires_confirmation is True
    assert preview.execution_supported is False
    assert len(preview.items) == 1
    assert preview.has_more is True
    assert preview.next_cursor == preview.items[0].placement_id
    assert preview.items[0].action == "migrate_managed"
    assert preview.items[0].old_mode == "copy"
    assert preview.items[0].new_mode == "copy"
    assert preview.items[0].old_path is not None
    assert preview.items[0].new_path is not None
    assert str(current_root) in preview.items[0].old_path
    assert str(proposed_root) in preview.items[0].new_path

    async with migration_db() as session:
        second_page = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=_policy(root_id, proposed_root),
            limit=1,
            after_placement_id=preview.next_cursor or 0,
        )
    assert second_page.scope_digest == preview.scope_digest
    assert second_page.total_placement_count == 3
    assert len(second_page.items) == 1
    assert second_page.items[0].placement_id > preview.items[0].placement_id
    assert second_page.has_more is True

    async with migration_db() as session:
        third_page = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=_policy(root_id, proposed_root),
            limit=1,
            after_placement_id=second_page.next_cursor or 0,
        )
    assert third_page.scope_digest == preview.scope_digest
    assert third_page.items[0].action == "preserve_referenced"
    assert third_page.items[0].old_path.endswith("user-owned.cbz")
    assert third_page.items[0].new_path == third_page.items[0].old_path
    assert third_page.items[0].new_mode == "reference_only"
    assert third_page.has_more is False
    assert third_page.next_cursor is None

    async with migration_db() as session:
        arc = await session.get(StoryArc, arc_id)
        placements = list((await session.scalars(select(StoryArcPlacement))).all())
    assert arc is not None
    assert arc.revision == 2
    assert arc.policy_snapshot["destination_root"] == str(current_root.resolve())
    assert all(Path(row.placement_path).exists() for row in placements)
    assert all(
        path.read_bytes() == f"canonical-{index}".encode()
        for index, path in enumerate(canonicals, 1)
    )


async def test_capacity_failure_blocks_without_inflating_collision_count(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    async with migration_db() as session:
        managed_files = list(
            (
                await session.scalars(
                    select(LibraryFile)
                    .join(
                        StoryArcPlacement,
                        StoryArcPlacement.library_file_id == LibraryFile.id,
                    )
                    .where(StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED)
                    .order_by(LibraryFile.id)
                )
            ).all()
        )
        for library_file in managed_files:
            library_file.file_size = 1
        await session.commit()
    monkeypatch.setattr(
        "pullbox.services.story_arc_policy_migration.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    async with migration_db() as session:
        preview = await StoryArcPolicyMigrationService().preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=_policy(root_id, proposed_root),
            limit=10,
        )

    assert preview.global_block_codes == ("insufficient_space",)
    assert preview.collision_count == 0
    assert preview.blocked_count == 0
    assert preview.required_bytes > len(managed_files)
    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as blocked:
            await StoryArcPolicyMigrationService().prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=_policy(root_id, proposed_root),
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert blocked.value.code == "policy_migration_blocked"


async def test_confirmation_is_actor_bound_exact_and_stale_on_scope_or_filesystem_change(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    service = StoryArcPolicyMigrationService()
    proposal = _policy(root_id, proposed_root)
    async with migration_db() as session:
        preview = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
            after_placement_id=0,
        )

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as wrong_actor:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=72,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert wrong_actor.value.code == "invalid_preview_token"

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as wrong_phrase:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation="CHANGE POLICY",
            )
    assert wrong_phrase.value.code == "confirmation_required"

    first_new_path = Path(
        next(item.new_path for item in preview.items if item.action == "migrate_managed") or ""
    )
    first_new_path.parent.mkdir(parents=True)
    first_new_path.write_bytes(b"untracked collision")
    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as stale:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert stale.value.code == "migration_preview_stale"

    async with migration_db() as session:
        refreshed = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
            after_placement_id=0,
        )
    assert refreshed.collision_count == 1
    assert refreshed.blocked_count == 1

    assert first_new_path.read_bytes() == b"untracked collision"
    async with migration_db() as session:
        arc = await session.get(StoryArc, arc_id)
    assert arc is not None and arc.revision == 2
    assert arc.policy_snapshot["destination_root"] == str(current_root.resolve())


async def test_active_recovery_fails_closed_before_preview(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    async with migration_db() as session:
        placement = await session.scalar(
            select(StoryArcPlacement)
            .where(StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED)
            .limit(1)
        )
        assert placement is not None
        placement.operation_token = "1" * 32
        placement.last_result = {
            "schema_version": 1,
            "status": "rename_recovery_required",
            "operation": "story_arc_reorder",
        }
        await session.commit()

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as blocked:
            await StoryArcPolicyMigrationService().preview_policy_change(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=_policy(root_id, proposed_root),
                limit=10,
                after_placement_id=0,
            )
    assert blocked.value.code == "placement_operation_recovery_pending"
    assert blocked.value.category == "conflict"


@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.COMPLETED, StoryArcSyncWorkState.CANCELLED],
)
async def test_terminal_sync_rollback_checkpoint_fails_closed(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    async with migration_db() as session:
        membership = await session.scalar(
            select(IssueStoryArc)
            .where(IssueStoryArc.story_arc_id == arc_id)
            .order_by(IssueStoryArc.id)
            .limit(1)
        )
        assert membership is not None and membership.issue_id is not None
        library_file = await session.scalar(
            select(LibraryFile)
            .where(LibraryFile.issue_id == membership.issue_id)
            .order_by(LibraryFile.id)
            .limit(1)
        )
        assert library_file is not None
        session.add(
            StoryArcSyncWork(
                issue_story_arc_id=membership.id,
                library_file_id=library_file.id,
                desired_generation="d" * 64,
                source_signature_hash="e" * 64,
                source_file_path=library_file.file_path,
                source_file_size=library_file.file_size,
                source_file_modified_at=library_file.file_modified_at,
                story_arc_revision=2,
                membership_sequence=membership.sequence_number,
                policy_schema_version=1,
                state=terminal_state,
                last_result={
                    "schema_version": 1,
                    "rollback": {
                        "schema_version": 1,
                        "status": "placement_removal_prepared",
                    },
                },
            )
        )
        await session.commit()

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as blocked:
            await StoryArcPolicyMigrationService().preview_policy_change(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=_policy(root_id, proposed_root),
                limit=10,
            )
    assert blocked.value.code == "placement_sync_work_pending"


async def test_same_path_mode_change_still_checks_proposed_mode_safety(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arc_id, root_id, current_root, _proposed_root, canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )

    def different_filesystem(path: Path) -> int:
        return 1 if path in canonicals else 2

    monkeypatch.setattr(
        policy_migration_module,
        "_filesystem_device",
        different_filesystem,
        raising=False,
    )
    proposal = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.HARDLINK,
        target_library_root_id=root_id,
        destination_root=str(current_root),
        folder_template="{StoryArc}",
        file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
        synchronize=True,
    )

    async with migration_db() as session:
        preview = await StoryArcPolicyMigrationService().preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
        )

    managed = [item for item in preview.items if item.ownership == "managed"]
    assert preview.managed_migrate_count == 2
    assert preview.blocked_count == 2
    assert preview.collision_count == 2
    assert {item.collision for item in managed} == {"cross_device"}


async def test_confirmation_binds_destination_root_filesystem_identity(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    service = StoryArcPolicyMigrationService()
    proposal = _policy(root_id, proposed_root)
    async with migration_db() as session:
        preview = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
        )

    retired_root = tmp_path / "retired-proposed-root"
    proposed_root.rename(retired_root)
    proposed_root.mkdir()

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as stale:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert stale.value.code == "migration_preview_stale"


async def test_scope_digest_binds_membership_and_canonical_db_truth(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    service = StoryArcPolicyMigrationService()
    proposal = _policy(root_id, proposed_root)
    async with migration_db() as session:
        preview = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
            after_placement_id=0,
        )

    async with migration_db() as session:
        membership = await session.scalar(
            select(IssueStoryArc)
            .where(IssueStoryArc.story_arc_id == arc_id)
            .order_by(IssueStoryArc.id)
            .limit(1)
        )
        canonical = await session.scalar(select(LibraryFile).order_by(LibraryFile.id).limit(1))
        assert membership is not None and canonical is not None
        membership.sequence_number = 40
        canonical.file_size += 1
        await session.commit()

    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as stale:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert stale.value.code == "migration_preview_stale"


async def test_preview_token_is_signed_and_timed(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.services.story_arc_policy_migration as migration_module

    arc_id, root_id, _current_root, proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    service = StoryArcPolicyMigrationService()
    proposal = _policy(root_id, proposed_root)
    async with migration_db() as session:
        preview = await service.preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=proposal,
            limit=10,
            after_placement_id=0,
        )

    replacement = "a" if preview.preview_token[0] != "a" else "b"
    tampered = f"{replacement}{preview.preview_token[1:]}"
    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as invalid:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=tampered,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert invalid.value.code == "invalid_preview_token"

    monkeypatch.setattr(migration_module, "_TOKEN_MAX_AGE_SECONDS", -1)
    async with migration_db() as session:
        with pytest.raises(StoryArcPolicyMigrationError) as expired:
            await service.prepare_confirmation(
                session,
                arc_id,
                actor_id=71,
                expected_revision=2,
                proposal=proposal,
                preview_token=preview.preview_token,
                confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            )
    assert expired.value.code == "migration_preview_expired"


async def test_logical_policy_preview_removes_only_managed_and_preserves_referenced(
    migration_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, _root_id, _current_root, _proposed_root, _canonicals = await _seed_migration_scope(
        migration_db, tmp_path
    )
    logical = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.LOGICAL,
        target_library_root_id=None,
        destination_root=None,
        folder_template="{StoryArc}",
        file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
        synchronize=False,
    )
    async with migration_db() as session:
        preview = await StoryArcPolicyMigrationService().preview_policy_change(
            session,
            arc_id,
            actor_id=71,
            expected_revision=2,
            proposal=logical,
            limit=10,
            after_placement_id=0,
        )
    assert preview.managed_migrate_count == 0
    assert preview.managed_remove_count == 2
    assert preview.referenced_preserved_count == 1
    assert preview.required_bytes == 0
    assert preview.blocked_count == 0
    assert {item.action for item in preview.items} == {
        "remove_managed",
        "preserve_referenced",
    }
    assert all(item.new_path is None for item in preview.items if item.action == "remove_managed")
    referenced = next(item for item in preview.items if item.action == "preserve_referenced")
    assert referenced.new_path == referenced.old_path
    assert referenced.new_mode == "reference_only"


def test_confirmation_phrase_is_exposed_as_an_exact_schema_constant() -> None:
    confirmation_schema = StoryArcPolicyMigrationConfirmationRequest.model_json_schema()[
        "properties"
    ]["confirmation"]
    assert confirmation_schema["const"] == STORY_ARC_POLICY_MIGRATION_CONFIRMATION


def test_terminal_sync_rollback_marker_requires_exact_settled_action_fence() -> None:
    safe_marker = {
        "schema_version": 1,
        "status": "cancelled_before_publish",
        "import_job_id": 3,
        "import_action_id": 5,
        "sync_work_id": 7,
        "membership_id": 11,
        "desired_generation": "d" * 64,
    }
    safe = _TerminalSyncWorkScope(
        id=7,
        last_result={"rollback": safe_marker},
        origin_import_job_id=3,
        origin_import_action_id=5,
        issue_story_arc_id=11,
        desired_generation="d" * 64,
        job_id=3,
        job_status=ImportJobStatus.ROLLED_BACK,
        action_id=5,
        action_import_job_id=3,
        action_status=ImportJobActionStatus.ROLLED_BACK,
    )
    malformed = _TerminalSyncWorkScope(
        id=7,
        last_result={"rollback": None},
        origin_import_job_id=3,
        origin_import_action_id=5,
        issue_story_arc_id=11,
        desired_generation="d" * 64,
        job_id=3,
        job_status=ImportJobStatus.ROLLED_BACK,
        action_id=5,
        action_import_job_id=3,
        action_status=ImportJobActionStatus.ROLLED_BACK,
    )

    assert _terminal_sync_recovery_pending(safe) is False
    assert _terminal_sync_recovery_pending(malformed) is True
    assert _terminal_sync_recovery_pending(replace(safe, action_import_job_id=4)) is True


def test_keyset_and_json_operation_queries_compile_for_sqlite_and_postgresql() -> None:
    membership = _membership_scope_statement(7, after_membership_id=11, limit=101)
    active = _active_operation_statement(7)
    terminal = _terminal_sync_work_statement(7, after_work_id=13, limit=101)

    sqlite_membership = str(membership.compile(dialect=sqlite.dialect()))
    postgres_membership = str(membership.compile(dialect=postgresql.dialect()))
    sqlite_active = str(active.compile(dialect=sqlite.dialect()))
    postgres_active = str(active.compile(dialect=postgresql.dialect()))
    sqlite_terminal = str(terminal.compile(dialect=sqlite.dialect()))
    postgres_terminal = str(terminal.compile(dialect=postgresql.dialect()))

    assert "issue_story_arcs.id >" in sqlite_membership
    assert "issue_story_arcs.id >" in postgres_membership
    assert "LIMIT" in sqlite_membership.upper()
    assert "LIMIT" in postgres_membership.upper()
    assert "JSON_EXTRACT" in sqlite_active
    assert "story_arc_placements.last_result ->>" in postgres_active
    assert "LIMIT" in sqlite_active.upper()
    assert "LIMIT" in postgres_active.upper()
    assert "story_arc_sync_work.id >" in sqlite_terminal
    assert "story_arc_sync_work.id >" in postgres_terminal
    assert "LEFT OUTER JOIN import_job_actions" in sqlite_terminal
    assert "LEFT OUTER JOIN import_job_actions" in postgres_terminal
