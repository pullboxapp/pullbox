"""Read-only planning for safe story-arc placement operations."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from pullbox.core.story_arc_naming import StoryArcNamingValues
from pullbox.services.story_arc_placement_preview import (
    StoryArcCollisionKind,
    StoryArcPlacementMode,
    StoryArcPlacementPreviewState,
    StoryArcSymlinkStyle,
    preview_story_arc_placement,
)

if TYPE_CHECKING:
    from pathlib import Path


def _values() -> StoryArcNamingValues:
    return StoryArcNamingValues(
        story_arc="Court of Owls",
        reading_order=1,
        series="Batman",
        issue_number="1AU",
        issue_title="The Court of Owls",
        year=2011,
        start_year=2011,
        end_year=2012,
        extension="cbz",
    )


def test_copy_preview_is_read_only_and_reports_required_bytes(tmp_path: Path) -> None:
    source = tmp_path / "library" / "Batman 1AU.cbz"
    source.parent.mkdir()
    source.write_bytes(b"canonical")
    root = tmp_path / "StoryArcs"
    root.mkdir()
    before = source.read_bytes()

    preview = preview_story_arc_placement(
        canonical_path=source,
        destination_root=root,
        values=_values(),
        mode=StoryArcPlacementMode.COPY,
    )

    assert preview.state == StoryArcPlacementPreviewState.READY
    assert preview.target_path == root / "Court of Owls" / (
        "001 - Batman 1AU - The Court of Owls.cbz"
    )
    assert preview.required_bytes == len(before)
    assert source.read_bytes() == before
    assert not preview.target_path.exists()


def test_reference_only_is_logical_and_has_no_target_path(tmp_path: Path) -> None:
    preview = preview_story_arc_placement(
        canonical_path=None,
        destination_root=None,
        values=_values(),
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
    )

    assert preview.state == StoryArcPlacementPreviewState.LOGICAL_ONLY
    assert preview.target_path is None
    assert preview.required_bytes == 0


def test_move_is_not_a_supported_arc_placement_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported story-arc placement mode"):
        preview_story_arc_placement(
            canonical_path=tmp_path / "source.cbz",
            destination_root=tmp_path,
            values=_values(),
            mode="move",
        )


def test_symlink_style_is_required_only_for_symlink_mode(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    root.mkdir()

    with pytest.raises(ValueError, match="requires a symlink style"):
        preview_story_arc_placement(
            canonical_path=source,
            destination_root=root,
            values=_values(),
            mode=StoryArcPlacementMode.SYMLINK,
        )
    with pytest.raises(ValueError, match="only valid for symlink"):
        preview_story_arc_placement(
            canonical_path=source,
            destination_root=root,
            values=_values(),
            mode=StoryArcPlacementMode.COPY,
            symlink_style=StoryArcSymlinkStyle.RELATIVE,
        )


def test_existing_different_target_is_a_blocking_collision(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target = root / "Court of Owls" / "001 - Batman 1AU - The Court of Owls.cbz"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different")

    preview = preview_story_arc_placement(
        canonical_path=source,
        destination_root=root,
        values=_values(),
        mode=StoryArcPlacementMode.COPY,
    )

    assert preview.state == StoryArcPlacementPreviewState.BLOCKED
    assert preview.collision == StoryArcCollisionKind.DIFFERENT_CONTENT
    assert preview.overwrite_allowed is False


def test_preexisting_same_inode_is_referenced_not_claimed_as_managed(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target = root / "Court of Owls" / "001 - Batman 1AU - The Court of Owls.cbz"
    target.parent.mkdir(parents=True)
    os.link(source, target)

    preview = preview_story_arc_placement(
        canonical_path=source,
        destination_root=root,
        values=_values(),
        mode=StoryArcPlacementMode.HARDLINK,
    )

    assert preview.state == StoryArcPlacementPreviewState.ALREADY_REPRESENTED
    assert preview.collision == StoryArcCollisionKind.SAME_INODE_REFERENCED
    assert preview.proposed_ownership == "referenced"


def test_existing_symlink_parent_cannot_escape_destination_root(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Court of Owls").symlink_to(outside, target_is_directory=True)

    preview = preview_story_arc_placement(
        canonical_path=source,
        destination_root=root,
        values=_values(),
        mode=StoryArcPlacementMode.COPY,
    )

    assert preview.state == StoryArcPlacementPreviewState.BLOCKED
    assert preview.collision == StoryArcCollisionKind.PATH_ESCAPE


def test_case_only_existing_name_is_reported_before_writes(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"canonical")
    root = tmp_path / "arcs"
    target_parent = root / "Court of Owls"
    target_parent.mkdir(parents=True)
    (target_parent / "001 - BATMAN 1AU - THE COURT OF OWLS.CBZ").write_bytes(b"other")

    preview = preview_story_arc_placement(
        canonical_path=source,
        destination_root=root,
        values=_values(),
        mode=StoryArcPlacementMode.COPY,
    )

    assert preview.state == StoryArcPlacementPreviewState.BLOCKED
    assert preview.collision == StoryArcCollisionKind.CASE_ONLY
