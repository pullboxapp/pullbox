"""Read-only safety and collision preview for story-arc placements."""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    StoryArcNamingValues,
    render_story_arc_relative_path,
)
from pullbox.models.story_arc import StoryArcPlacementMode, StoryArcSymlinkStyle

if TYPE_CHECKING:
    from pathlib import Path


class StoryArcPlacementPreviewState(enum.StrEnum):
    READY = "ready"
    LOGICAL_ONLY = "logical_only"
    ALREADY_REPRESENTED = "already_represented"
    BLOCKED = "blocked"


class StoryArcCollisionKind(enum.StrEnum):
    NONE = "none"
    DIFFERENT_CONTENT = "different_content"
    SAME_INODE_REFERENCED = "same_inode_referenced"
    CASE_ONLY = "case_only"
    PATH_ESCAPE = "path_escape"
    CROSS_DEVICE = "cross_device"
    ROOT_UNAVAILABLE = "root_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreview:
    """Truthful read-only result for one proposed placement."""

    state: StoryArcPlacementPreviewState
    mode: StoryArcPlacementMode
    target_path: Path | None
    collision: StoryArcCollisionKind = StoryArcCollisionKind.NONE
    reason: str | None = None
    required_bytes: int = 0
    proposed_ownership: str = "managed"
    overwrite_allowed: bool = False


def preview_story_arc_placement(
    *,
    canonical_path: Path | None,
    destination_root: Path | None,
    values: StoryArcNamingValues,
    mode: StoryArcPlacementMode | str,
    symlink_style: StoryArcSymlinkStyle | str | None = None,
    folder_template: str | None = None,
    file_template: str | None = None,
) -> StoryArcPlacementPreview:
    """Plan one arc placement without creating, renaming, or deleting anything."""
    try:
        effective_mode = StoryArcPlacementMode(mode)
    except ValueError as exc:
        msg = f"Unsupported story-arc placement mode: {mode}"
        raise ValueError(msg) from exc

    effective_symlink_style: StoryArcSymlinkStyle | None = None
    if symlink_style is not None:
        try:
            effective_symlink_style = StoryArcSymlinkStyle(symlink_style)
        except ValueError as exc:
            msg = f"Unsupported story-arc symlink style: {symlink_style}"
            raise ValueError(msg) from exc
    if effective_mode == StoryArcPlacementMode.SYMLINK and effective_symlink_style is None:
        msg = "Story-arc symlink mode requires a symlink style"
        raise ValueError(msg)
    if effective_mode != StoryArcPlacementMode.SYMLINK and effective_symlink_style is not None:
        msg = "Story-arc symlink style is only valid for symlink mode"
        raise ValueError(msg)

    if effective_mode == StoryArcPlacementMode.REFERENCE_ONLY:
        return StoryArcPlacementPreview(
            state=StoryArcPlacementPreviewState.LOGICAL_ONLY,
            mode=effective_mode,
            target_path=None,
            proposed_ownership="referenced",
        )

    if canonical_path is None or not canonical_path.exists() or not canonical_path.is_file():
        return _blocked(
            effective_mode,
            None,
            StoryArcCollisionKind.SOURCE_UNAVAILABLE,
            "Canonical issue file is unavailable",
        )
    if canonical_path.is_symlink():
        return _blocked(
            effective_mode,
            None,
            StoryArcCollisionKind.SOURCE_UNAVAILABLE,
            "Canonical issue file is a symbolic link and requires explicit review",
        )
    if destination_root is None or not destination_root.is_absolute():
        return _blocked(
            effective_mode,
            None,
            StoryArcCollisionKind.ROOT_UNAVAILABLE,
            "Story-arc destination root is unavailable",
        )
    if not destination_root.exists() or not destination_root.is_dir():
        return _blocked(
            effective_mode,
            None,
            StoryArcCollisionKind.ROOT_UNAVAILABLE,
            "Story-arc destination root is unavailable",
        )
    if not os.access(destination_root, os.R_OK | os.W_OK | os.X_OK):
        return _blocked(
            effective_mode,
            None,
            StoryArcCollisionKind.ROOT_UNAVAILABLE,
            "Story-arc destination root is not readable and writable",
        )

    relative_path = render_story_arc_relative_path(
        values,
        folder_template=(
            DEFAULT_STORY_ARC_FOLDER_TEMPLATE if folder_template is None else folder_template
        ),
        file_template=(DEFAULT_STORY_ARC_FILE_TEMPLATE if file_template is None else file_template),
    )
    target_path = destination_root / relative_path
    resolved_root = destination_root.resolve(strict=True)
    resolved_target = target_path.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        return _blocked(
            effective_mode,
            target_path,
            StoryArcCollisionKind.PATH_ESCAPE,
            "Rendered story-arc path resolves outside the selected root",
        )

    case_collision = _find_case_only_collision(target_path)
    if case_collision is not None:
        return _blocked(
            effective_mode,
            target_path,
            StoryArcCollisionKind.CASE_ONLY,
            "A case-only destination collision already exists",
        )

    if target_path.exists() or target_path.is_symlink():
        try:
            same_inode = target_path.exists() and target_path.samefile(canonical_path)
        except OSError:
            same_inode = False
        if same_inode:
            return StoryArcPlacementPreview(
                state=StoryArcPlacementPreviewState.ALREADY_REPRESENTED,
                mode=effective_mode,
                target_path=target_path,
                collision=StoryArcCollisionKind.SAME_INODE_REFERENCED,
                reason="An existing user artifact already references the canonical file",
                proposed_ownership="referenced",
            )
        return _blocked(
            effective_mode,
            target_path,
            StoryArcCollisionKind.DIFFERENT_CONTENT,
            "A different artifact already exists at the rendered destination",
        )

    source_stat = canonical_path.stat()
    if effective_mode == StoryArcPlacementMode.HARDLINK:
        destination_device = _nearest_existing_ancestor(target_path.parent).stat().st_dev
        if source_stat.st_dev != destination_device:
            return _blocked(
                effective_mode,
                target_path,
                StoryArcCollisionKind.CROSS_DEVICE,
                "Hardlink source and destination are on different filesystems",
            )

    return StoryArcPlacementPreview(
        state=StoryArcPlacementPreviewState.READY,
        mode=effective_mode,
        target_path=target_path,
        required_bytes=(source_stat.st_size if effective_mode == StoryArcPlacementMode.COPY else 0),
    )


def _find_case_only_collision(target_path: Path) -> Path | None:
    parent = target_path.parent
    if not parent.exists() or not parent.is_dir():
        return None
    target_key = target_path.name.casefold()
    try:
        for child in parent.iterdir():
            if child.name != target_path.name and child.name.casefold() == target_key:
                return child
    except OSError:
        return None
    return None


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def _blocked(
    mode: StoryArcPlacementMode,
    target_path: Path | None,
    collision: StoryArcCollisionKind,
    reason: str,
) -> StoryArcPlacementPreview:
    return StoryArcPlacementPreview(
        state=StoryArcPlacementPreviewState.BLOCKED,
        mode=mode,
        target_path=target_path,
        collision=collision,
        reason=reason,
    )
