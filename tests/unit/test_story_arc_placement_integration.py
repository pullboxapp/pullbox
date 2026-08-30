"""Database boundary for managed story-arc placement synchronization."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
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
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'placements.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_membership(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    arc_name: str = "Court of Owls",
    issue_number_text: str = "1000000",
    publisher_name: str | None = None,
) -> tuple[int, int, int, Path, Path]:
    canonical = tmp_path / "library" / "Batman.cbz"
    canonical.parent.mkdir(exist_ok=True)
    canonical.write_bytes(b"canonical archive")
    arc_root = tmp_path / "arcs"
    arc_root.mkdir(exist_ok=True)
    now = datetime.now(UTC)

    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path / "library"), enabled=True)
        publisher = Publisher(name=publisher_name) if publisher_name is not None else None
        series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2011,
            library_root=root,
            publisher=publisher,
        )
        issue = Issue(
            series=series,
            issue_number=float(issue_number_text),
            issue_number_text=issue_number_text,
            title="The Court of Owls",
        )
        library_file = LibraryFile(
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=now,
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        arc = StoryArc(name=arc_name, source_kind=StoryArcSourceKind.PULLBOX)
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.PULLBOX,
            source_issue_number_text=issue_number_text,
        )
        session.add_all([library_file, membership])
        await session.commit()
        return arc.id, membership.id, root.id, canonical, arc_root


def _copy_policy(root_id: int, arc_root: Path) -> StoryArcPlacementPolicyInput:
    return StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.COPY,
        target_library_root_id=root_id,
        destination_root=str(arc_root),
        folder_template="{StoryArc}",
        file_template=("{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"),
        symlink_style=None,
        synchronize=True,
    )


async def test_policy_freezes_complete_snapshot_and_rejects_stale_revision(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, _canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()

    async with db_factory() as session:
        policy = await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    assert policy.configured is True
    assert policy.revision == 2
    assert policy.snapshot == {
        "schema_version": 1,
        "mode": "copy",
        "target_library_root_id": root_id,
        "destination_root": str(arc_root.resolve()),
        "folder_template": "{StoryArc}",
        "file_template": ("{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"),
        "symlink_style": None,
        "synchronize": True,
    }

    async with db_factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None and membership.sync_eligible is True
        with pytest.raises(StoryArcPlacementIntegrationError) as stale:
            await service.update_policy(
                session,
                arc_id,
                expected_revision=1,
                proposal=_copy_policy(root_id, arc_root),
            )
    assert stale.value.code == "revision_conflict"


async def test_publisher_folder_token_uses_canonical_series_publisher(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, _membership_id, root_id, _canonical, arc_root = await _seed_membership(
        db_factory,
        tmp_path,
        publisher_name="DC Comics",
    )
    service = StoryArcPlacementSyncService()
    proposal = _copy_policy(root_id, arc_root)
    proposal = StoryArcPlacementPolicyInput(
        mode=proposal.mode,
        target_library_root_id=proposal.target_library_root_id,
        destination_root=proposal.destination_root,
        folder_template="{Publisher} - {StoryArc}",
        file_template=proposal.file_template,
        symlink_style=proposal.symlink_style,
        synchronize=proposal.synchronize,
    )

    async with db_factory() as session:
        preview = await service.preview_arc(
            session,
            arc_id,
            limit=1,
            offset=0,
            proposal=proposal,
        )

    target = Path(preview.items[0].target_path or "")
    assert target.parent.name == "DC Comics - Court of Owls"


@pytest.mark.parametrize(
    ("proposal", "code"),
    [
        (
            StoryArcPlacementPolicyInput(
                mode=StoryArcPlacementPolicyMode.COPY,
                target_library_root_id=1,
                destination_root="/tmp",
                folder_template="../{StoryArc}",
                file_template="{Series} {IssueNumber}",
                synchronize=False,
            ),
            "invalid_folder_template",
        ),
        (
            StoryArcPlacementPolicyInput(
                mode=StoryArcPlacementPolicyMode.SYMLINK,
                target_library_root_id=1,
                destination_root="/tmp",
                folder_template="{StoryArc}",
                file_template="{Series} {IssueNumber}",
                synchronize=False,
            ),
            "symlink_style_required",
        ),
    ],
)
async def test_policy_validation_fails_closed(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    proposal: StoryArcPlacementPolicyInput,
    code: str,
) -> None:
    arc_id, _membership_id, _root_id, _canonical, _arc_root = await _seed_membership(
        db_factory, tmp_path
    )

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as error:
            await StoryArcPlacementSyncService().update_policy(
                session,
                arc_id,
                expected_revision=1,
                proposal=proposal,
            )
    assert error.value.code == code


async def test_preview_and_copy_sync_preserve_exact_issue_and_are_idempotent(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    async with db_factory() as session:
        preview = await service.preview_arc(session, arc_id, limit=25, offset=0)
    assert preview.total == 1
    assert preview.items[0].membership_id == membership_id
    assert preview.items[0].target_path is not None
    assert "1000000" in preview.items[0].target_path
    assert "e+" not in preview.items[0].target_path.casefold()

    async with db_factory() as session:
        first = await service.sync_membership(session, arc_id, membership_id)
    assert first.outcome == "created"
    assert first.placement is not None
    target = Path(first.placement.placement_path)
    assert target.read_bytes() == canonical.read_bytes()
    assert canonical.exists()

    async with db_factory() as session:
        second = await service.sync_membership(session, arc_id, membership_id)
    assert second.outcome == "idempotent"
    assert second.placement is not None
    assert second.placement.id == first.placement.id
    assert target.read_bytes() == canonical.read_bytes()


async def test_preview_classifies_managed_current_missing_and_drifted_without_mutation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synchronized = await service.sync_membership(session, arc_id, membership_id)
    assert synchronized.placement is not None
    placement_id = synchronized.placement.id
    target = Path(synchronized.placement.placement_path)
    original_target = target.read_bytes()

    async with db_factory() as session:
        current = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert current.items[0].classification == "managed_current"
    assert current.items[0].placement_id == placement_id
    assert current.items[0].current_ownership == "managed"
    assert current.items[0].state == "already_represented"
    assert current.items[0].collision == "none"

    target.unlink()
    async with db_factory() as session:
        missing = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert missing.items[0].classification == "managed_missing"
    assert missing.items[0].placement_id == placement_id
    assert missing.items[0].state == "missing"
    assert missing.items[0].collision == "managed_missing"
    assert missing.items[0].inspection_code == "target_missing"
    assert not target.exists()

    target.write_bytes(b"user replacement")
    replacement = target.read_bytes()
    async with db_factory() as session:
        drifted = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert drifted.items[0].classification == "managed_drifted"
    assert drifted.items[0].placement_id == placement_id
    assert drifted.items[0].state == "drifted"
    assert drifted.items[0].collision == "different_content"
    assert drifted.items[0].inspection_code == "target_fingerprint_mismatch"
    assert target.read_bytes() == replacement
    assert canonical.read_bytes() == original_target

    async with db_factory() as session:
        persisted = await session.get(StoryArcPlacement, placement_id)
    assert persisted is not None
    assert persisted.state is StoryArcPlacementState.CURRENT
    assert persisted.last_result["status"] == "complete"


async def test_preview_classifies_referenced_current_missing_and_changed_read_only(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    reference = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.REFERENCE_ONLY,
        target_library_root_id=root_id,
        destination_root=str(arc_root),
        synchronize=True,
    )
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=reference,
        )
        initial = await service.preview_arc(session, arc_id, limit=1, offset=0)
    target = Path(initial.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())
    async with db_factory() as session:
        adopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert adopted.placement is not None
    placement_id = adopted.placement.id

    async with db_factory() as session:
        current = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert current.items[0].classification == "referenced_current"
    assert current.items[0].placement_id == placement_id
    assert current.items[0].current_ownership == "referenced"
    assert current.items[0].state == "already_represented"
    assert current.items[0].proposed_ownership == "referenced"

    target.unlink()
    async with db_factory() as session:
        missing = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert missing.items[0].classification == "referenced_missing"
    assert missing.items[0].state == "missing"
    assert missing.items[0].collision == "reference_missing"
    assert missing.items[0].inspection_code == "target_missing"
    assert not target.exists()

    target.write_bytes(b"changed referenced artifact")
    changed = target.read_bytes()
    async with db_factory() as session:
        drifted = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert drifted.items[0].classification == "referenced_drifted"
    assert drifted.items[0].state == "drifted"
    assert drifted.items[0].collision == "different_content"
    assert drifted.items[0].inspection_code == "target_fingerprint_mismatch"
    assert target.read_bytes() == changed

    async with db_factory() as session:
        persisted = await session.get(StoryArcPlacement, placement_id)
    assert persisted is not None
    assert persisted.state is StoryArcPlacementState.CURRENT
    assert persisted.last_result["status"] == "complete"


async def test_preview_detects_managed_representation_drift_without_rewriting_link(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    symlink_policy = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.SYMLINK,
        target_library_root_id=root_id,
        destination_root=str(arc_root),
        symlink_style=StoryArcSymlinkStyle.RELATIVE,
        synchronize=True,
    )
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=symlink_policy,
        )
        synchronized = await service.sync_membership(session, arc_id, membership_id)
    assert synchronized.placement is not None
    target = Path(synchronized.placement.placement_path)
    assert target.is_symlink()
    target.unlink()
    target.symlink_to(canonical)
    absolute_link_target = target.readlink()
    assert absolute_link_target.is_absolute()

    async with db_factory() as session:
        drifted = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert drifted.items[0].classification == "managed_drifted"
    assert drifted.items[0].state == "drifted"
    assert drifted.items[0].collision == "representation_changed"
    assert drifted.items[0].inspection_code == "representation_changed"
    assert target.is_symlink()
    assert target.readlink() == absolute_link_target
    assert target.resolve(strict=True) == canonical.resolve(strict=True)


async def test_preview_distinguishes_untracked_identical_from_different_content(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, _membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        free = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert free.items[0].classification == "will_materialize"
    target = Path(free.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())
    identical_inode = target.stat().st_ino

    async with db_factory() as session:
        identical = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert identical.items[0].classification == "untracked_identical"
    assert identical.items[0].placement_id is None
    assert identical.items[0].current_ownership is None
    assert identical.items[0].state == "blocked"
    assert identical.items[0].collision == "identical_unconfirmed"
    assert identical.items[0].inspection_code == "untracked_identical"
    assert identical.items[0].proposed_ownership == "referenced"
    assert target.stat().st_ino == identical_inode

    target.write_bytes(b"different user artifact")
    different = target.read_bytes()
    async with db_factory() as session:
        collision = await service.preview_arc(session, arc_id, limit=1, offset=0)
        placement_count = int(await session.scalar(select(func.count(StoryArcPlacement.id))) or 0)
    assert collision.items[0].classification == "different_content"
    assert collision.items[0].state == "blocked"
    assert collision.items[0].collision == "different_content"
    assert placement_count == 0
    assert target.read_bytes() == different


async def test_preview_loads_only_matching_placement_evidence_for_bounded_page(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        free = await service.preview_arc(session, arc_id, limit=1, offset=0)
    target = Path(free.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())

    async with db_factory() as session:
        exact = StoryArcPlacement(
            issue_story_arc_id=membership_id,
            library_file_id=None,
            library_root_id=root_id,
            placement_path=str(target),
            mode=StoryArcPlacementMode.REFERENCE_ONLY,
            ownership=StoryArcPlacementOwnership.REFERENCED,
            source_kind=StoryArcSourceKind.PULLBOX,
            rendered_reading_order=1,
            policy_schema_version=1,
            source_fingerprint={},
            state=StoryArcPlacementState.CURRENT,
            last_result={},
        )
        session.add(exact)
        for index in range(25):
            session.add(
                StoryArcPlacement(
                    issue_story_arc_id=membership_id,
                    library_file_id=None,
                    library_root_id=root_id,
                    placement_path=str(arc_root / f"stale-{index}.cbz"),
                    mode=StoryArcPlacementMode.REFERENCE_ONLY,
                    ownership=StoryArcPlacementOwnership.REFERENCED,
                    source_kind=StoryArcSourceKind.PULLBOX,
                    rendered_reading_order=index + 2,
                    policy_schema_version=1,
                    source_fingerprint={},
                    state=StoryArcPlacementState.DRIFTED,
                    last_result={},
                )
            )
        await session.commit()
        exact_id = exact.id

    loaded_placement_ids: list[int] = []

    def record_load(placement: StoryArcPlacement, _context: object) -> None:
        loaded_placement_ids.append(placement.id)

    event.listen(StoryArcPlacement, "load", record_load)
    try:
        async with db_factory() as session:
            preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    finally:
        event.remove(StoryArcPlacement, "load", record_load)

    assert preview.total == 1
    assert preview.items[0].placement_id == exact_id
    assert loaded_placement_ids == [exact_id]


async def test_failed_copy_is_persisted_without_changing_canonical_state(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    def fail_copy(*_args: object, **_kwargs: object) -> object:
        from pullbox.services.story_arc_placement_service import StoryArcPlacementSafetyError

        raise StoryArcPlacementSafetyError("copy_failed", "Copy could not be published")

    monkeypatch.setattr(
        "pullbox.services.story_arc_placement_integration.execute_story_arc_placement",
        fail_copy,
    )
    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as failure:
            await service.sync_membership(session, arc_id, membership_id)
    assert failure.value.code == "copy_failed"
    assert canonical.read_bytes() == b"canonical archive"

    async with db_factory() as session:
        issue_count = int(await session.scalar(select(func.count(Issue.id))) or 0)
        file_count = int(await session.scalar(select(func.count(LibraryFile.id))) or 0)
        placement = await session.scalar(select(StoryArcPlacement))
        membership = await session.get(IssueStoryArc, membership_id)
    assert issue_count == 1
    assert file_count == 1
    assert placement is not None
    assert placement.state == StoryArcPlacementState.FAILED
    assert placement.last_result["error_code"] == "copy_failed"
    assert membership is not None
    assert membership.last_materialization_result["error_code"] == "copy_failed"


async def test_concurrent_sync_creates_one_row_and_one_artifact(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, _canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    async def synchronize() -> str:
        async with db_factory() as session:
            result = await service.sync_membership(session, arc_id, membership_id)
            return result.outcome

    outcomes = await asyncio.gather(synchronize(), synchronize())
    assert sorted(outcomes) == ["created", "idempotent"]
    async with db_factory() as session:
        assert int(await session.scalar(select(func.count(StoryArcPlacement.id))) or 0) == 1
        placement = await session.scalar(select(StoryArcPlacement))
    assert placement is not None
    assert placement.ownership == StoryArcPlacementOwnership.MANAGED
    assert len(list(arc_root.rglob("*.cbz"))) == 1


async def test_one_canonical_issue_can_sync_into_two_arcs(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    first_arc_id, first_membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    async with db_factory() as session:
        first_membership = await session.get(IssueStoryArc, first_membership_id)
        assert first_membership is not None
        second_arc = StoryArc(name="Night of the Owls", source_kind=StoryArcSourceKind.PULLBOX)
        second_membership = IssueStoryArc(
            story_arc=second_arc,
            issue_id=first_membership.issue_id,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.PULLBOX,
            source_issue_number_text="1000000",
        )
        session.add(second_membership)
        await session.commit()
        second_arc_id = second_arc.id
        second_membership_id = second_membership.id

    service = StoryArcPlacementSyncService()
    for arc_id in (first_arc_id, second_arc_id):
        async with db_factory() as session:
            await service.update_policy(
                session,
                arc_id,
                expected_revision=1,
                proposal=_copy_policy(root_id, arc_root),
            )
    for arc_id, membership_id in (
        (first_arc_id, first_membership_id),
        (second_arc_id, second_membership_id),
    ):
        async with db_factory() as session:
            await service.sync_membership(session, arc_id, membership_id)

    assert canonical.read_bytes() == b"canonical archive"
    assert len(list(arc_root.rglob("*.cbz"))) == 2
    async with db_factory() as session:
        rows = list((await session.scalars(select(StoryArcPlacement))).all())
    assert len(rows) == 2
    assert {row.issue_story_arc_id for row in rows} == {
        first_membership_id,
        second_membership_id,
    }


async def test_explicit_adoption_keeps_identical_user_artifact_referenced(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    target = Path(preview.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())
    inode_before = target.stat().st_ino

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as unconfirmed:
            await service.sync_membership(session, arc_id, membership_id)
    assert unconfirmed.value.code == "identical_unconfirmed"
    async with db_factory() as session:
        assert int(await session.scalar(select(func.count(StoryArcPlacement.id))) or 0) == 0

    async with db_factory() as session:
        adopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert adopted.outcome == "referenced_existing"
    assert adopted.placement is not None
    assert adopted.placement.ownership == StoryArcPlacementOwnership.REFERENCED
    assert target.stat().st_ino == inode_before


async def test_missing_managed_placement_can_be_repaired(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synced = await service.sync_membership(session, arc_id, membership_id)
    assert synced.placement is not None
    target = Path(synced.placement.placement_path)
    target.unlink()

    async with db_factory() as session:
        repaired = await service.repair_placement(
            session,
            arc_id,
            synced.placement.id,
        )
    assert repaired.outcome == "created"
    assert target.read_bytes() == canonical.read_bytes()


async def test_managed_placement_can_be_removed_without_touching_canonical_file(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synced = await service.sync_membership(session, arc_id, membership_id)
    assert synced.placement is not None
    target = Path(synced.placement.placement_path)

    async with db_factory() as session:
        removed = await service.remove_placement(
            session,
            arc_id,
            synced.placement.id,
            confirm_managed_artifact_removal=True,
        )

    assert removed.placement_id == synced.placement.id
    assert removed.ownership is StoryArcPlacementOwnership.MANAGED
    assert removed.artifact_removed is True
    assert removed.canonical_preserved is True
    assert removed.referenced_artifact_preserved is False
    assert canonical.read_bytes() == b"canonical archive"
    assert not target.exists()
    async with db_factory() as session:
        assert await session.get(StoryArcPlacement, synced.placement.id) is None
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        assert membership.sync_eligible is False

    async with db_factory() as session:
        recreated = await service.sync_membership(session, arc_id, membership_id)
    assert recreated.outcome == "created"
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        assert membership.sync_eligible is True


@pytest.mark.parametrize(
    ("checkpoint_status", "checkpoint_token"),
    [
        ("published_pending_reconcile", "different-checkpoint-token"),
        ("prepared", "placement-operation-token"),
    ],
)
async def test_abandoned_publish_takeover_requires_exact_published_checkpoint(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    checkpoint_status: str,
    checkpoint_token: str,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synchronized = await service.sync_membership(session, arc_id, membership_id)
        assert synchronized.placement is not None
        placement_id = int(synchronized.placement.id)
        placement = await session.get(StoryArcPlacement, placement_id)
        assert placement is not None
        target = Path(placement.placement_path)
        target_fingerprint = dict(placement.last_result)["target_fingerprint"]
        placement.operation_token = "placement-operation-token"
        placement.last_result = {
            "schema_version": 1,
            "status": checkpoint_status,
            "operation_token": checkpoint_token,
            "target_fingerprint": target_fingerprint,
        }
        await session.commit()

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as blocked:
            await service.remove_placement(
                session,
                arc_id,
                placement_id,
                confirm_managed_artifact_removal=True,
                abandoned_published_operation_token="placement-operation-token",
            )

    assert blocked.value.code == "placement_published_operation_token_invalid"
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        retained = await session.get(StoryArcPlacement, placement_id)
        assert retained is not None
        assert retained.operation_token == "placement-operation-token"


async def test_live_published_checkpoint_requires_explicit_takeover_authority(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synchronized = await service.sync_membership(session, arc_id, membership_id)
        assert synchronized.placement is not None
        placement_id = int(synchronized.placement.id)
        placement = await session.get(StoryArcPlacement, placement_id)
        assert placement is not None
        target = Path(placement.placement_path)
        target_fingerprint = dict(placement.last_result)["target_fingerprint"]
        placement.operation_token = "live-published-token"
        placement.last_result = {
            "schema_version": 1,
            "status": "published_pending_reconcile",
            "operation_token": "live-published-token",
            "target_fingerprint": target_fingerprint,
        }
        await session.commit()

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as blocked:
            await service.remove_placement(
                session,
                arc_id,
                placement_id,
                confirm_managed_artifact_removal=True,
            )

    assert blocked.value.code == "placement_operation_in_progress"
    assert target.read_bytes() == canonical.read_bytes()


async def test_referenced_placement_record_removal_preserves_user_artifact(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    target = Path(preview.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())

    async with db_factory() as session:
        adopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert adopted.placement is not None
    assert adopted.placement.ownership is StoryArcPlacementOwnership.REFERENCED

    async with db_factory() as session:
        removed = await service.remove_placement(session, arc_id, adopted.placement.id)

    assert removed.ownership is StoryArcPlacementOwnership.REFERENCED
    assert removed.artifact_removed is False
    assert removed.canonical_preserved is True
    assert removed.referenced_artifact_preserved is True
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        assert await session.get(StoryArcPlacement, adopted.placement.id) is None
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        assert membership.sync_eligible is False

    async with db_factory() as session:
        readopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert readopted.placement is not None
    assert readopted.placement.ownership is StoryArcPlacementOwnership.REFERENCED
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        assert membership.sync_eligible is True


async def test_referenced_forget_waits_for_in_flight_sync_and_cannot_be_undone(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.services.story_arc_placement_integration as integration

    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    target = Path(preview.items[0].target_path or "")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())
    async with db_factory() as session:
        adopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert adopted.placement is not None

    started = threading.Event()
    release = threading.Event()
    execute = integration.execute_story_arc_placement

    def delayed_execute(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=5)
        return execute(*args, **kwargs)

    monkeypatch.setattr(integration, "execute_story_arc_placement", delayed_execute)

    async def recheck_reference() -> object:
        async with db_factory() as session:
            return await service.sync_membership(session, arc_id, membership_id)

    async def forget_reference() -> object:
        async with db_factory() as session:
            return await service.remove_placement(session, arc_id, adopted.placement.id)

    sync_task = asyncio.create_task(recheck_reference())
    assert await asyncio.to_thread(started.wait, 5)
    forget_task = asyncio.create_task(forget_reference())
    await asyncio.sleep(0.05)
    forgot_before_sync_finished = forget_task.done()
    release.set()
    await sync_task
    removed = await forget_task

    assert forgot_before_sync_finished is False
    assert removed.referenced_artifact_preserved is True
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        assert await session.get(StoryArcPlacement, adopted.placement.id) is None
        membership = await session.get(IssueStoryArc, membership_id)
        assert membership is not None
        assert membership.sync_eligible is False


async def test_changed_managed_placement_is_not_removed(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synced = await service.sync_membership(session, arc_id, membership_id)
    assert synced.placement is not None
    target = Path(synced.placement.placement_path)
    target.write_bytes(b"user changed this arc artifact")

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as blocked:
            await service.remove_placement(
                session,
                arc_id,
                synced.placement.id,
                confirm_managed_artifact_removal=True,
            )

    assert blocked.value.code == "fingerprint_mismatch"
    assert blocked.value.category == "safety"
    assert canonical.read_bytes() == b"canonical archive"
    assert target.read_bytes() == b"user changed this arc artifact"
    async with db_factory() as session:
        assert await session.get(StoryArcPlacement, synced.placement.id) is not None


async def test_managed_removal_recovers_after_unlink_before_database_checkpoint(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.services.story_arc_placement_integration as integration

    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        synced = await service.sync_membership(session, arc_id, membership_id)
    assert synced.placement is not None
    target = Path(synced.placement.placement_path)
    checkpoint = integration._delete_removed_placement_checkpoint

    async def crash_after_unlink(*_args: object, **_kwargs: object) -> None:
        raise StoryArcPlacementIntegrationError(
            "remove_checkpoint_interrupted",
            "Simulated crash after managed placement unlink",
            category="conflict",
        )

    monkeypatch.setattr(
        integration,
        "_delete_removed_placement_checkpoint",
        crash_after_unlink,
    )
    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError, match="Simulated crash"):
            await service.remove_placement(
                session,
                arc_id,
                synced.placement.id,
                confirm_managed_artifact_removal=True,
            )
    assert not target.exists()
    assert canonical.read_bytes() == b"canonical archive"

    async with db_factory() as session:
        prepared = await session.get(StoryArcPlacement, synced.placement.id)
        assert prepared is not None
        assert prepared.last_result["status"] == "remove_prepared"
        prepared.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()

    monkeypatch.setattr(
        integration,
        "_delete_removed_placement_checkpoint",
        checkpoint,
    )
    async with db_factory() as session:
        recovered = await service.remove_placement(
            session,
            arc_id,
            synced.placement.id,
            confirm_managed_artifact_removal=True,
        )
    assert recovered.artifact_removed is False
    assert canonical.read_bytes() == b"canonical archive"
    async with db_factory() as session:
        assert await session.get(StoryArcPlacement, synced.placement.id) is None


async def test_managed_policy_destination_change_is_blocked_until_migrated(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, _canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
        await service.sync_membership(session, arc_id, membership_id)

    replacement_root = tmp_path / "replacement-arcs"
    replacement_root.mkdir()
    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as blocked:
            await service.update_policy(
                session,
                arc_id,
                expected_revision=2,
                proposal=_copy_policy(root_id, replacement_root),
            )
    assert blocked.value.code == "managed_policy_change_requires_migration"
    assert blocked.value.category == "conflict"


async def test_publish_checkpoint_recovers_after_reconcile_crash(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.services.story_arc_placement_integration as integration

    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    reconcile = integration._reconcile_result

    async def crash_after_publish(*_args: object, **_kwargs: object) -> object:
        raise StoryArcPlacementIntegrationError(
            "reconcile_interrupted",
            "Simulated crash after filesystem publish",
            category="conflict",
        )

    monkeypatch.setattr(integration, "_reconcile_result", crash_after_publish)
    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError, match="Simulated crash"):
            await service.sync_membership(session, arc_id, membership_id)

    target = next(arc_root.rglob("*.cbz"))
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        prepared = await session.scalar(select(StoryArcPlacement))
    assert prepared is not None
    assert prepared.last_result["status"] == "published_pending_reconcile"
    assert prepared.source_fingerprint
    assert prepared.last_result["target_fingerprint"]

    monkeypatch.setattr(integration, "_reconcile_result", reconcile)
    async with db_factory() as session:
        recovered = await service.sync_membership(session, arc_id, membership_id)
    assert recovered.outcome == "idempotent"
    assert recovered.placement is not None
    assert recovered.placement.ownership == StoryArcPlacementOwnership.MANAGED
    assert len(list(arc_root.rglob("*.cbz"))) == 1


async def test_prepared_ownership_recovers_after_publish_before_checkpoint_crash(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart must recover the publish/checkpoint crash window safely."""
    import pullbox.services.story_arc_placement_integration as integration

    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    checkpoint = integration._persist_published_checkpoint

    async def crash_before_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise StoryArcPlacementIntegrationError(
            "checkpoint_interrupted",
            "Simulated crash before the published checkpoint",
            category="conflict",
        )

    monkeypatch.setattr(integration, "_persist_published_checkpoint", crash_before_checkpoint)
    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError, match="Simulated crash"):
            await service.sync_membership(session, arc_id, membership_id)

    target = next(arc_root.rglob("*.cbz"))
    assert target.read_bytes() == canonical.read_bytes()
    async with db_factory() as session:
        prepared = await session.scalar(select(StoryArcPlacement))
    assert prepared is not None
    assert prepared.last_result["status"] == "prepared"
    assert prepared.source_fingerprint

    # A normal concurrent caller must not steal a live reservation.  Simulate
    # startup reconciliation after the prior process lease has expired.
    async with db_factory() as session:
        stale = await session.get(StoryArcPlacement, prepared.id)
        assert stale is not None
        stale.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()

    monkeypatch.setattr(integration, "_persist_published_checkpoint", checkpoint)
    async with db_factory() as session:
        recovered = await service.sync_membership(session, arc_id, membership_id)
    assert recovered.outcome == "referenced_existing"
    assert recovered.placement is not None
    assert recovered.placement.ownership == StoryArcPlacementOwnership.REFERENCED
    assert recovered.placement.state == StoryArcPlacementState.CURRENT
    assert len(list(arc_root.rglob("*.cbz"))) == 1


async def test_cancelled_sync_is_truthful_and_publishes_nothing(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as cancelled:
            await service.sync_membership(
                session,
                arc_id,
                membership_id,
                cancellation_requested=lambda: True,
            )
    assert cancelled.value.code == "cancelled"
    assert cancelled.value.category == "cancelled"
    assert canonical.read_bytes() == b"canonical archive"
    assert not list(arc_root.rglob("*.cbz"))
    async with db_factory() as session:
        placement = await session.scalar(select(StoryArcPlacement))
        membership = await session.get(IssueStoryArc, membership_id)
    assert placement is None
    assert membership is not None
    assert membership.last_materialization_result["error_category"] == "cancelled"


async def test_task_cancellation_waits_for_publish_checkpoint_and_reconciliation(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.services.story_arc_placement_integration as integration

    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )

    started = threading.Event()
    release = threading.Event()
    execute = integration.execute_story_arc_placement

    def delayed_execute(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=5)
        return execute(*args, **kwargs)

    monkeypatch.setattr(integration, "execute_story_arc_placement", delayed_execute)
    async with db_factory() as session:
        task = asyncio.create_task(service.sync_membership(session, arc_id, membership_id))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert canonical.read_bytes() == b"canonical archive"
    assert len(list(arc_root.rglob("*.cbz"))) == 1
    async with db_factory() as session:
        placement = await session.scalar(select(StoryArcPlacement))
    assert placement is not None
    assert placement.state == StoryArcPlacementState.CURRENT
    assert placement.last_result["status"] == "complete"
    assert placement.last_result["target_fingerprint"]


async def test_logical_and_reference_only_have_distinct_safe_sync_semantics(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    service = StoryArcPlacementSyncService()
    logical = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.LOGICAL,
        target_library_root_id=None,
        destination_root=None,
        synchronize=False,
    )
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=logical,
        )
        preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert preview.items[0].mode == "logical"
    assert preview.items[0].target_path is None
    async with db_factory() as session:
        logical_result = await service.sync_membership(session, arc_id, membership_id)
    assert logical_result.outcome == "logical"
    assert logical_result.placement is None

    reference = StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.REFERENCE_ONLY,
        target_library_root_id=root_id,
        destination_root=str(arc_root),
        synchronize=True,
    )
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=2,
            proposal=reference,
        )
    target = arc_root / "Court of Owls" / ("001 - Batman 1000000 - The Court of Owls.cbz")
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical.read_bytes())
    async with db_factory() as session:
        reference_preview = await service.preview_arc(session, arc_id, limit=1, offset=0)
    assert reference_preview.items[0].mode == "reference_only"
    assert reference_preview.items[0].target_path == str(target)
    assert reference_preview.items[0].proposed_ownership == "referenced"

    async with db_factory() as session:
        with pytest.raises(StoryArcPlacementIntegrationError) as confirmation:
            await service.sync_membership(session, arc_id, membership_id)
    assert confirmation.value.code == "reference_adoption_required"
    async with db_factory() as session:
        adopted = await service.sync_membership(
            session,
            arc_id,
            membership_id,
            adopt_identical_existing=True,
        )
    assert adopted.outcome == "referenced_existing"
    assert adopted.placement is not None
    assert adopted.placement.ownership == StoryArcPlacementOwnership.REFERENCED
    assert target.read_bytes() == canonical.read_bytes()


async def test_preview_and_placement_lists_are_server_paginated(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, first_membership_id, root_id, _canonical, arc_root = await _seed_membership(
        db_factory, tmp_path
    )
    membership_ids = [first_membership_id]
    async with db_factory() as session:
        arc = await session.get(StoryArc, arc_id)
        root = await session.get(LibraryRoot, root_id)
        series = await session.scalar(select(Series).limit(1))
        assert arc is not None and root is not None and series is not None
        for number in (2, 3):
            canonical = tmp_path / "library" / f"Batman {number}.cbz"
            canonical.write_bytes(f"canonical {number}".encode())
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
            session.add_all([library_file, membership])
            await session.flush()
            membership_ids.append(membership.id)
        await session.commit()

    service = StoryArcPlacementSyncService()
    async with db_factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=_copy_policy(root_id, arc_root),
        )
    async with db_factory() as session:
        preview = await service.preview_arc(session, arc_id, limit=1, offset=1)
    assert preview.total == 3
    assert preview.limit == 1
    assert preview.offset == 1
    assert preview.has_more is True
    assert [item.membership_id for item in preview.items] == [membership_ids[1]]

    for membership_id in membership_ids:
        async with db_factory() as session:
            await service.sync_membership(session, arc_id, membership_id)
    async with db_factory() as session:
        placements = await service.list_placements(session, arc_id, limit=1, offset=1)
    assert placements.total == 3
    assert placements.limit == 1
    assert placements.offset == 1
    assert placements.has_more is True
    assert placements.items[0].issue_story_arc_id == membership_ids[1]
