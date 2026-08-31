"""Filesystem safety contract for managed story-arc placements."""

from __future__ import annotations

import errno
import os
import stat
from typing import TYPE_CHECKING

import pytest

from pullbox.core.story_arc_naming import StoryArcNamingValues
from pullbox.models.story_arc import (
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcSymlinkStyle,
)
from pullbox.services import story_arc_placement_service as placement_service
from pullbox.services.story_arc_placement_service import (
    MAX_STORY_ARC_PLACEMENTS_PER_BATCH,
    ManagedStoryArcPlacementEvidence,
    StoryArcPlacementCancellationError,
    StoryArcPlacementCollisionError,
    StoryArcPlacementInspectionEvidence,
    StoryArcPlacementInspectionState,
    StoryArcPlacementJournalEvent,
    StoryArcPlacementOwnershipError,
    StoryArcPlacementPlan,
    StoryArcPlacementResult,
    StoryArcPlacementResultState,
    StoryArcPlacementSafetyError,
    execute_story_arc_placement,
    execute_story_arc_placement_batch,
    inspect_story_arc_placement,
    remove_managed_story_arc_placement,
    repair_managed_story_arc_placement,
)

if TYPE_CHECKING:
    from pathlib import Path


def _values(
    *,
    arc: str = "Court of Owls",
    issue_number: str = "1",
    reading_order: int = 1,
) -> StoryArcNamingValues:
    return StoryArcNamingValues(
        story_arc=arc,
        reading_order=reading_order,
        series="Batman",
        issue_number=issue_number,
        issue_title="The Court of Owls",
        year=2011,
        extension="cbz",
    )


def _plan(
    source: Path,
    root: Path,
    *,
    mode: StoryArcPlacementMode = StoryArcPlacementMode.COPY,
    style: StoryArcSymlinkStyle | None = None,
    arc: str = "Court of Owls",
    issue_number: str = "1",
    membership_id: int = 41,
    adopt_identical_existing: bool = False,
) -> StoryArcPlacementPlan:
    return StoryArcPlacementPlan(
        issue_story_arc_id=membership_id,
        library_file_id=73,
        canonical_path=source,
        destination_root=root,
        values=_values(arc=arc, issue_number=issue_number),
        mode=mode,
        symlink_style=style,
        adopt_identical_existing=adopt_identical_existing,
    )


def test_reference_only_is_a_bounded_noop(tmp_path: Path) -> None:
    root = tmp_path / "arcs"
    source = tmp_path / "missing.cbz"

    result = execute_story_arc_placement(
        _plan(source, root, mode=StoryArcPlacementMode.REFERENCE_ONLY)
    )

    assert result.state is StoryArcPlacementResultState.REFERENCE_ONLY
    assert result.ownership is StoryArcPlacementOwnership.REFERENCED
    assert result.target_path is None
    assert result.source_fingerprint == {}
    assert not root.exists()


def test_copy_publishes_atomically_and_preserves_exact_large_issue_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "library" / "Batman.cbz"
    source.parent.mkdir()
    source.write_bytes(b"canonical archive")
    root = tmp_path / "arcs"
    root.mkdir()

    result = execute_story_arc_placement(_plan(source, root, issue_number="1000000"))

    assert result.state is StoryArcPlacementResultState.CREATED
    assert result.ownership is StoryArcPlacementOwnership.MANAGED
    assert result.target_path is not None
    assert "1000000" in result.target_path.name
    assert "e+" not in result.target_path.name.lower()
    assert result.target_path.read_bytes() == b"canonical archive"
    assert source.read_bytes() == b"canonical archive"
    assert result.source_fingerprint["sha256"] == result.target_fingerprint["sha256"]
    assert not list(result.target_path.parent.glob(".pullbox-story-arc-*.tmp"))


def test_cancelled_copy_cleans_destination_local_temporary_file(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    root = tmp_path / "arcs"
    root.mkdir()

    def cancel_when_temp_exists() -> bool:
        return any(root.rglob(".pullbox-story-arc-*.tmp"))

    with pytest.raises(StoryArcPlacementCancellationError):
        execute_story_arc_placement(
            _plan(source, root),
            cancellation_requested=cancel_when_temp_exists,
        )

    assert source.exists()
    assert not list(root.rglob(".pullbox-story-arc-*.tmp"))
    assert not list(root.rglob("*.cbz"))


def test_hardlink_has_no_copy_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    real_link = os.link

    def fail_link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if str(dst).endswith("cbz"):
            raise OSError(errno.EXDEV, "cross-device link")
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(StoryArcPlacementSafetyError, match="Hardlink") as error:
        execute_story_arc_placement(_plan(source, root, mode=StoryArcPlacementMode.HARDLINK))

    assert error.value.code == "cross_device"
    assert not list(root.rglob("*.cbz"))
    assert source.read_bytes() == b"canonical"


def test_hardlink_is_the_same_inode_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()

    result = execute_story_arc_placement(_plan(source, root, mode=StoryArcPlacementMode.HARDLINK))

    assert result.target_path is not None
    assert result.target_path.samefile(source)
    assert source.read_bytes() == b"canonical"


@pytest.mark.parametrize(
    "style",
    [StoryArcSymlinkStyle.ABSOLUTE, StoryArcSymlinkStyle.RELATIVE],
)
def test_symlink_style_resolves_to_canonical_file(
    tmp_path: Path,
    style: StoryArcSymlinkStyle,
) -> None:
    source = tmp_path / "library" / "source.cbz"
    source.parent.mkdir()
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()

    result = execute_story_arc_placement(
        _plan(source, root, mode=StoryArcPlacementMode.SYMLINK, style=style)
    )

    assert result.target_path is not None
    assert result.target_path.is_symlink()
    assert result.target_path.resolve(strict=True) == source.resolve(strict=True)
    if style is StoryArcSymlinkStyle.ABSOLUTE:
        assert os.path.isabs(os.readlink(result.target_path))
    else:
        assert not os.path.isabs(os.readlink(result.target_path))


def test_execution_rechecks_symlink_parent_escape(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Court of Owls").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, root))

    assert error.value.code in {"path_escape", "symlink_parent"}
    assert not list(outside.iterdir())


def test_existing_different_content_and_case_collisions_are_never_overwritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target_parent = root / "Court of Owls"
    target_parent.mkdir(parents=True)
    collision = target_parent / "001 - Batman 1 - The Court of Owls.cbz"
    collision.write_bytes(b"user bytes")

    with pytest.raises(StoryArcPlacementCollisionError) as error:
        execute_story_arc_placement(_plan(source, root))

    assert error.value.code == "different_content"
    assert collision.read_bytes() == b"user bytes"

    collision.rename(target_parent / collision.name.upper())
    with pytest.raises(StoryArcPlacementCollisionError) as case_error:
        execute_story_arc_placement(_plan(source, root))
    assert case_error.value.code == "case_only"


def test_confirmed_identical_user_artifact_stays_referenced(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    inode_before = target.stat().st_ino

    result = execute_story_arc_placement(_plan(source, root, adopt_identical_existing=True))

    assert result.state is StoryArcPlacementResultState.REFERENCED_EXISTING
    assert result.ownership is StoryArcPlacementOwnership.REFERENCED
    assert target.stat().st_ino == inode_before
    assert target.read_bytes() == b"canonical"


def test_same_inode_user_artifact_still_requires_explicit_adoption(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    target.parent.mkdir(parents=True)
    os.link(source, target)

    with pytest.raises(StoryArcPlacementCollisionError) as error:
        execute_story_arc_placement(_plan(source, root, mode=StoryArcPlacementMode.HARDLINK))
    assert error.value.code == "identical_unconfirmed"
    assert target.samefile(source)

    adopted = execute_story_arc_placement(
        _plan(
            source,
            root,
            mode=StoryArcPlacementMode.HARDLINK,
            adopt_identical_existing=True,
        )
    )
    assert adopted.state is StoryArcPlacementResultState.REFERENCED_EXISTING
    assert adopted.ownership is StoryArcPlacementOwnership.REFERENCED


def test_symlink_destination_root_is_rejected_before_writes(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    physical_root = tmp_path / "physical-arcs"
    physical_root.mkdir()
    configured_root = tmp_path / "arcs"
    configured_root.symlink_to(physical_root, target_is_directory=True)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, configured_root))

    assert error.value.code == "symlink_root"
    assert not list(physical_root.rglob("*.cbz"))


def test_destination_root_retarget_after_preview_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    moved_root = tmp_path / "original-arcs"
    outside = tmp_path / "outside"
    outside.mkdir()

    def retarget_root(event: StoryArcPlacementJournalEvent) -> None:
        if event.stage == "prepared":
            root.rename(moved_root)
            root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, root), journal=retarget_root)

    assert error.value.code in {"root_changed", "symlink_root"}
    assert not list(outside.rglob("*.cbz"))


def test_copy_publish_uses_pinned_parent_when_path_is_retargeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    original_parent = root / "Court of Owls"
    moved_parent = root / "original-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_link = os.link
    retargeted = False

    def retarget_before_link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        nonlocal retargeted
        if str(dst).endswith(".cbz") and not retargeted:
            retargeted = True
            original_parent.rename(moved_parent)
            original_parent.symlink_to(outside, target_is_directory=True)
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", retarget_before_link)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, root))

    assert error.value.code == "parent_changed"
    assert not list(outside.iterdir())
    assert not list(moved_parent.glob("*.cbz"))
    assert source.read_bytes() == b"canonical"


def test_failed_publish_cleanup_preserves_a_replacement_user_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    real_assert_stable = placement_service._assert_parent_path_stable
    replaced = False

    def replace_before_validation(
        parent: placement_service._SecureParentDirectory,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            target.unlink()
            target.write_bytes(b"user replacement")
            raise StoryArcPlacementSafetyError(
                "parent_changed",
                "Story-arc destination parent changed during execution",
            )
        real_assert_stable(parent)

    monkeypatch.setattr(placement_service, "_assert_parent_path_stable", replace_before_validation)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, root))

    assert error.value.code == "parent_changed"
    assert target.read_bytes() == b"user replacement"
    assert source.read_bytes() == b"canonical"


def test_failed_publish_cleanup_preserves_same_inode_same_size_user_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"

    def edit_before_validation(parent: placement_service._SecureParentDirectory) -> None:
        before = target.stat()
        target.write_bytes(b"user edit")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_ino == before.st_ino
        assert target.stat().st_size == before.st_size
        raise StoryArcPlacementSafetyError("parent_changed", "Simulated post-publication failure")

    monkeypatch.setattr(placement_service, "_assert_parent_path_stable", edit_before_validation)
    with pytest.raises(StoryArcPlacementSafetyError, match="Simulated post-publication failure"):
        execute_story_arc_placement(_plan(source, root))

    assert target.read_bytes() == b"user edit"
    assert source.read_bytes() == b"canonical"


@pytest.mark.parametrize("change", ["before_hash", "after_hash", "unreadable"])
def test_failed_publish_cleanup_rechecks_content_and_preserves_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    real_fingerprint = placement_service._fingerprint_regular_at

    def fail_validation(parent: placement_service._SecureParentDirectory) -> None:
        raise StoryArcPlacementSafetyError("parent_changed", "Original publication failure")

    def change_during_cleanup(parent_fd: int, name: str) -> dict[str, object]:
        if change == "unreadable":
            raise StoryArcPlacementSafetyError("fingerprint_mismatch", "Cannot verify ownership")
        if change == "before_hash":
            target.write_bytes(b"user edit")
        fingerprint = real_fingerprint(parent_fd, name)
        if change == "after_hash":
            target.write_bytes(b"user edit")
        return fingerprint

    monkeypatch.setattr(placement_service, "_assert_parent_path_stable", fail_validation)
    monkeypatch.setattr(placement_service, "_fingerprint_regular_at", change_during_cleanup)
    with pytest.raises(StoryArcPlacementSafetyError, match="Original publication failure"):
        execute_story_arc_placement(_plan(source, root))

    assert target.read_bytes() == (b"canonical" if change == "unreadable" else b"user edit")
    assert source.read_bytes() == b"canonical"


@pytest.mark.parametrize(
    "mode",
    [StoryArcPlacementMode.COPY, StoryArcPlacementMode.HARDLINK, StoryArcPlacementMode.SYMLINK],
)
def test_failed_publish_cleans_up_unchanged_owned_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: StoryArcPlacementMode
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"

    def fail_validation(parent: placement_service._SecureParentDirectory) -> None:
        raise StoryArcPlacementSafetyError("parent_changed", "Original publication failure")

    monkeypatch.setattr(placement_service, "_assert_parent_path_stable", fail_validation)
    style = StoryArcSymlinkStyle.ABSOLUTE if mode is StoryArcPlacementMode.SYMLINK else None
    with pytest.raises(StoryArcPlacementSafetyError, match="Original publication failure"):
        execute_story_arc_placement(_plan(source, root, mode=mode, style=style))

    assert not target.exists()
    assert not target.is_symlink()
    assert source.read_bytes() == b"canonical"


def test_retry_of_unchanged_managed_copy_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    plan = _plan(source, root)
    created = execute_story_arc_placement(plan)
    evidence = ManagedStoryArcPlacementEvidence.from_result(
        created,
        creating_action_id=919,
    )
    assert created.target_path is not None
    inode_before = created.target_path.stat().st_ino

    retried = execute_story_arc_placement(plan, existing_managed=evidence)

    assert retried.state is StoryArcPlacementResultState.IDEMPOTENT
    assert retried.target_path == created.target_path
    assert retried.target_path.stat().st_ino == inode_before


def test_remove_and_repair_require_managed_ownership_and_fingerprint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    plan = _plan(source, root)
    created = execute_story_arc_placement(plan)
    managed = ManagedStoryArcPlacementEvidence.from_result(created, creating_action_id=22)
    assert created.target_path is not None

    created.target_path.unlink()
    repaired = repair_managed_story_arc_placement(plan, managed)
    assert repaired.state is StoryArcPlacementResultState.CREATED
    assert created.target_path.read_bytes() == b"canonical"

    drifted_bytes = b"user changed this"
    created.target_path.write_bytes(drifted_bytes)
    with pytest.raises(StoryArcPlacementSafetyError) as drift_error:
        remove_managed_story_arc_placement(
            managed,
            destination_root=root,
            canonical_path=source,
        )
    assert drift_error.value.code == "fingerprint_mismatch"
    assert created.target_path.read_bytes() == drifted_bytes
    assert source.read_bytes() == b"canonical"

    referenced = ManagedStoryArcPlacementEvidence(
        issue_story_arc_id=managed.issue_story_arc_id,
        placement_path=managed.placement_path,
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
        symlink_style=None,
        source_fingerprint=managed.source_fingerprint,
        target_fingerprint=managed.target_fingerprint,
        creating_action_id=None,
    )
    with pytest.raises(StoryArcPlacementOwnershipError):
        remove_managed_story_arc_placement(
            referenced,
            destination_root=root,
            canonical_path=source,
        )
    assert created.target_path.read_bytes() == drifted_bytes


def test_validated_managed_removal_never_removes_canonical_file(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    created = execute_story_arc_placement(_plan(source, root, mode=StoryArcPlacementMode.HARDLINK))
    managed = ManagedStoryArcPlacementEvidence.from_result(created, creating_action_id=23)

    removed = remove_managed_story_arc_placement(
        managed,
        destination_root=root,
        canonical_path=source,
    )

    assert removed.removed is True
    assert created.target_path is not None and not created.target_path.exists()
    assert source.read_bytes() == b"canonical"


def test_managed_removal_rechecks_root_after_journal_boundary(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    created = execute_story_arc_placement(_plan(source, root))
    managed = ManagedStoryArcPlacementEvidence.from_result(created, creating_action_id=24)
    moved_root = tmp_path / "original-arcs"
    outside = tmp_path / "outside"
    outside_target = outside / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_bytes(b"user artifact")

    def retarget_root(event: StoryArcPlacementJournalEvent) -> None:
        if event.stage == "remove_prepared":
            root.rename(moved_root)
            root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        remove_managed_story_arc_placement(
            managed,
            destination_root=root,
            canonical_path=source,
            journal=retarget_root,
        )

    assert error.value.code in {"root_changed", "symlink_root"}
    assert outside_target.read_bytes() == b"user artifact"
    assert created.target_path is not None
    assert (moved_root / created.target_path.relative_to(root)).read_bytes() == b"canonical"
    assert source.read_bytes() == b"canonical"


def test_managed_remove_uses_pinned_parent_when_path_is_retargeted_inside_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    created = execute_story_arc_placement(_plan(source, root))
    managed = ManagedStoryArcPlacementEvidence.from_result(created, creating_action_id=25)
    assert created.target_path is not None
    original_parent = created.target_path.parent
    moved_parent = root / "original-parent"
    outside = tmp_path / "outside"
    outside_target = outside / created.target_path.name
    outside.mkdir()
    outside_target.write_bytes(b"user artifact")
    real_unlink = os.unlink
    retargeted = False

    def retarget_before_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal retargeted
        if str(path) == created.target_path.name and not retargeted:
            retargeted = True
            original_parent.rename(moved_parent)
            original_parent.symlink_to(outside, target_is_directory=True)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", retarget_before_unlink)

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        remove_managed_story_arc_placement(
            managed,
            destination_root=root,
            canonical_path=source,
        )

    assert error.value.code == "parent_changed"
    assert outside_target.read_bytes() == b"user artifact"
    assert source.read_bytes() == b"canonical"


def test_publish_and_remove_fsync_file_and_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    real_fsync = os.fsync
    fsynced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    created = execute_story_arc_placement(_plan(source, root))
    managed = ManagedStoryArcPlacementEvidence.from_result(created, creating_action_id=26)
    remove_managed_story_arc_placement(
        managed,
        destination_root=root,
        canonical_path=source,
    )

    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) >= 3


@pytest.mark.parametrize(
    ("mode", "style"),
    [
        (StoryArcPlacementMode.COPY, None),
        (StoryArcPlacementMode.HARDLINK, None),
        (StoryArcPlacementMode.SYMLINK, StoryArcSymlinkStyle.ABSOLUTE),
        (StoryArcPlacementMode.SYMLINK, StoryArcSymlinkStyle.RELATIVE),
    ],
)
def test_every_publish_mode_fsyncs_its_final_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: StoryArcPlacementMode,
    style: StoryArcSymlinkStyle | None,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    (root / "Court of Owls").mkdir(parents=True)
    real_fsync = os.fsync
    directory_fsyncs = 0

    def record_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    result = execute_story_arc_placement(_plan(source, root, mode=mode, style=style))

    assert result.target_path is not None and result.target_path.exists()
    assert directory_fsyncs >= 1


def test_managed_publish_fails_closed_without_secure_directory_fd_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    monkeypatch.setattr(
        "pullbox.services.story_arc_placement_service._secure_dir_fd_supported",
        lambda: False,
    )

    with pytest.raises(StoryArcPlacementSafetyError) as error:
        execute_story_arc_placement(_plan(source, root))

    assert error.value.code == "secure_dir_fd_unavailable"
    assert not list(root.rglob("*.cbz"))


def _inspection_evidence(
    result: StoryArcPlacementResult,
    *,
    ownership: StoryArcPlacementOwnership | None = None,
) -> StoryArcPlacementInspectionEvidence:
    target_path = result.target_path
    assert target_path is not None
    return StoryArcPlacementInspectionEvidence(
        placement_path=target_path,
        mode=result.mode,
        ownership=result.ownership if ownership is None else ownership,
        symlink_style=result.symlink_style,
        source_fingerprint=dict(result.source_fingerprint),
        target_fingerprint=dict(result.target_fingerprint),
    )


def test_read_only_inspection_reports_free_current_and_missing_without_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    plan = _plan(source, root)

    free = inspect_story_arc_placement(plan)
    assert free.state is StoryArcPlacementInspectionState.FREE
    assert not list(root.rglob("*.cbz"))

    created = execute_story_arc_placement(plan)
    evidence = _inspection_evidence(created)
    assert created.target_path is not None
    inode = created.target_path.stat().st_ino

    current = inspect_story_arc_placement(plan, existing=evidence)
    assert current.state is StoryArcPlacementInspectionState.MANAGED_CURRENT
    assert current.code is None
    assert created.target_path.stat().st_ino == inode

    created.target_path.unlink()
    missing = inspect_story_arc_placement(plan, existing=evidence)
    assert missing.state is StoryArcPlacementInspectionState.MANAGED_MISSING
    assert missing.code == "target_missing"
    assert not created.target_path.exists()


def test_read_only_inspection_distinguishes_representation_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "library" / "source.cbz"
    source.parent.mkdir()
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    plan = _plan(
        source,
        root,
        mode=StoryArcPlacementMode.SYMLINK,
        style=StoryArcSymlinkStyle.RELATIVE,
    )
    created = execute_story_arc_placement(plan)
    evidence = _inspection_evidence(created)
    assert created.target_path is not None
    created.target_path.unlink()
    created.target_path.symlink_to(source)

    inspected = inspect_story_arc_placement(plan, existing=evidence)

    assert inspected.state is StoryArcPlacementInspectionState.MANAGED_DRIFTED
    assert inspected.code == "representation_changed"
    assert created.target_path.is_symlink()
    assert created.target_path.resolve(strict=True) == source


def test_read_only_inspection_handles_referenced_and_untracked_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target = root / "Court of Owls" / "001 - Batman 1 - The Court of Owls.cbz"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    plan = _plan(source, root, adopt_identical_existing=True)

    untracked = inspect_story_arc_placement(plan)
    assert untracked.state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL

    adopted = execute_story_arc_placement(plan)
    evidence = _inspection_evidence(adopted)
    current = inspect_story_arc_placement(plan, existing=evidence)
    assert current.state is StoryArcPlacementInspectionState.REFERENCED_CURRENT

    target.write_bytes(b"changed")
    drifted = inspect_story_arc_placement(plan, existing=evidence)
    assert drifted.state is StoryArcPlacementInspectionState.REFERENCED_DRIFTED
    assert drifted.code in {"target_fingerprint_mismatch", "content_changed"}

    different = inspect_story_arc_placement(plan)
    assert different.state is StoryArcPlacementInspectionState.DIFFERENT_CONTENT


def test_one_canonical_issue_can_be_placed_in_multiple_arcs(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()

    results = execute_story_arc_placement_batch(
        [
            _plan(source, root, arc="Court of Owls", membership_id=1),
            _plan(source, root, arc="Night of the Owls", membership_id=2),
        ]
    )

    assert len(results) == 2
    assert {result.issue_story_arc_id for result in results} == {1, 2}
    assert all(result.target_path and result.target_path.exists() for result in results)


def test_batch_is_bounded_before_any_filesystem_write(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    plan = _plan(source, root)

    with pytest.raises(ValueError, match="bounded"):
        execute_story_arc_placement_batch([plan] * (MAX_STORY_ARC_PLACEMENTS_PER_BATCH + 1))

    assert not list(root.rglob("*.cbz"))


def test_journal_callback_straddles_managed_publish_boundary(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()
    events: list[StoryArcPlacementJournalEvent] = []

    result = execute_story_arc_placement(_plan(source, root), journal=events.append)

    assert result.target_path is not None and result.target_path.exists()
    assert [event.stage for event in events] == ["prepared", "published"]
    assert events[0].target_path == result.target_path
    assert events[1].target_fingerprint == result.target_fingerprint
