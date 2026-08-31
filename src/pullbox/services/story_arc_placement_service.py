"""Bounded filesystem execution for optional story-arc placements.

This module deliberately owns no database transaction.  Callers pass an
immutable plan, persist the returned fingerprint evidence, and may adapt the
small journal callback to the import action journal.  Canonical files are read
only; story-arc materialization never has a move operation.
"""

from __future__ import annotations

import enum
import errno
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pullbox.models.story_arc import (
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcSymlinkStyle,
)
from pullbox.services.story_arc_placement_preview import (
    StoryArcCollisionKind,
    StoryArcPlacementPreview,
    StoryArcPlacementPreviewState,
    preview_story_arc_placement,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pullbox.core.story_arc_naming import StoryArcNamingValues

MAX_STORY_ARC_PLACEMENTS_PER_BATCH = 250
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_CASE_SCAN_ENTRIES = 10_000
_TEMP_PREFIX = ".pullbox-story-arc-"
_TEMP_SUFFIX = ".tmp"
_SECURE_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and os.scandir in os.supports_fd
    and os.utime in os.supports_fd
    and all(
        operation in os.supports_dir_fd
        for operation in (
            os.open,
            os.mkdir,
            os.stat,
            os.unlink,
            os.link,
            os.symlink,
            os.readlink,
        )
    )
)

Fingerprint = dict[str, object]


@dataclass(frozen=True, slots=True)
class _PublishedTarget:
    """Ephemeral ownership evidence captured from the artifact being published."""

    stat: os.stat_result
    sha256: str | None = None
    link_target: str | None = None


class StoryArcPlacementError(RuntimeError):
    """Base class for a categorized placement failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class StoryArcPlacementSafetyError(StoryArcPlacementError):
    """A root, path, source, or mode precondition failed closed."""


class StoryArcPlacementCollisionError(StoryArcPlacementError):
    """A destination exists that Pullbox may not overwrite."""


class StoryArcPlacementCancellationError(StoryArcPlacementError):
    """Execution observed cancellation before publishing an artifact."""

    def __init__(self) -> None:
        super().__init__("cancelled", "Story-arc placement was cancelled")


class StoryArcPlacementOwnershipError(StoryArcPlacementError):
    """A destructive operation lacks managed ownership evidence."""


class StoryArcPlacementResultState(enum.StrEnum):
    """Stable result values without coupling callers to an ORM state enum."""

    CREATED = "created"
    IDEMPOTENT = "idempotent"
    REFERENCED_EXISTING = "referenced_existing"
    REFERENCE_ONLY = "reference_only"


class StoryArcPlacementInspectionState(enum.StrEnum):
    """Stable read-only states for placement lifecycle presentation."""

    FREE = "free"
    MANAGED_CURRENT = "managed_current"
    REFERENCED_CURRENT = "referenced_current"
    MANAGED_MISSING = "managed_missing"
    REFERENCED_MISSING = "referenced_missing"
    MANAGED_DRIFTED = "managed_drifted"
    REFERENCED_DRIFTED = "referenced_drifted"
    UNTRACKED_IDENTICAL = "untracked_identical"
    DIFFERENT_CONTENT = "different_content"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPlan:
    """Complete data-only plan for one resolved story-arc membership."""

    issue_story_arc_id: int
    library_file_id: int | None
    canonical_path: Path | None
    destination_root: Path | None
    values: StoryArcNamingValues
    mode: StoryArcPlacementMode | str
    symlink_style: StoryArcSymlinkStyle | str | None = None
    folder_template: str | None = None
    file_template: str | None = None
    adopt_identical_existing: bool = False


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreparation:
    """Pre-publish evidence that an async caller can commit durably.

    The target fingerprint cannot exist until after publication.  Persisting
    the immutable source, destination-root identity, target, mode, and
    operation token lets a restart distinguish a prepared managed operation
    from an untracked user artifact and validate the result before adopting it.
    """

    issue_story_arc_id: int
    target_path: Path
    mode: StoryArcPlacementMode
    symlink_style: StoryArcSymlinkStyle | None
    rendered_reading_order: int
    source_fingerprint: Fingerprint
    destination_root_fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class PreparedManagedStoryArcPlacementEvidence:
    """Durably committed ownership intent for publish-window recovery."""

    issue_story_arc_id: int
    placement_path: Path
    mode: StoryArcPlacementMode | str
    symlink_style: StoryArcSymlinkStyle | str | None
    source_fingerprint: Fingerprint
    destination_root_fingerprint: Fingerprint
    operation_token: str


@dataclass(frozen=True, slots=True)
class StoryArcPlacementResult:
    """Persistence-ready evidence for one completed or no-op placement."""

    issue_story_arc_id: int
    library_file_id: int | None
    state: StoryArcPlacementResultState
    target_path: Path | None
    mode: StoryArcPlacementMode
    ownership: StoryArcPlacementOwnership
    symlink_style: StoryArcSymlinkStyle | None
    rendered_reading_order: int
    source_fingerprint: Fingerprint
    target_fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class StoryArcPlacementInspectionEvidence:
    """Persisted target expectation used only for read-only inspection."""

    placement_path: Path
    mode: StoryArcPlacementMode | str
    ownership: StoryArcPlacementOwnership | str
    symlink_style: StoryArcSymlinkStyle | str | None
    source_fingerprint: Fingerprint
    target_fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class StoryArcPlacementInspection:
    """Pure filesystem observation with a stable state and reason code."""

    state: StoryArcPlacementInspectionState
    mode: StoryArcPlacementMode
    target_path: Path | None
    collision: StoryArcCollisionKind = StoryArcCollisionKind.NONE
    code: str | None = None
    reason: str | None = None
    required_bytes: int = 0
    proposed_ownership: str = "managed"
    source_fingerprint: Fingerprint | None = None
    target_fingerprint: Fingerprint | None = None


@dataclass(frozen=True, slots=True)
class ManagedStoryArcPlacementEvidence:
    """Previously persisted evidence required for retry, repair, or removal."""

    issue_story_arc_id: int
    placement_path: Path
    mode: StoryArcPlacementMode | str
    ownership: StoryArcPlacementOwnership | str
    symlink_style: StoryArcSymlinkStyle | str | None
    source_fingerprint: Fingerprint
    target_fingerprint: Fingerprint
    creating_action_id: int | None

    @classmethod
    def from_result(
        cls,
        result: StoryArcPlacementResult,
        *,
        creating_action_id: int,
    ) -> ManagedStoryArcPlacementEvidence:
        """Create durable evidence after the caller records a publish action."""
        if result.ownership is not StoryArcPlacementOwnership.MANAGED or result.target_path is None:
            raise StoryArcPlacementOwnershipError(
                "not_managed",
                "Only a managed placement result can produce managed evidence",
            )
        return cls(
            issue_story_arc_id=result.issue_story_arc_id,
            placement_path=result.target_path,
            mode=result.mode,
            ownership=result.ownership,
            symlink_style=result.symlink_style,
            source_fingerprint=dict(result.source_fingerprint),
            target_fingerprint=dict(result.target_fingerprint),
            creating_action_id=creating_action_id,
        )


@dataclass(frozen=True, slots=True)
class StoryArcPlacementJournalEvent:
    """Narrow event that an import/action-journal adapter can persist."""

    stage: Literal["prepared", "published", "failed", "remove_prepared", "removed"]
    operation: Literal["publish", "remove"]
    issue_story_arc_id: int
    mode: StoryArcPlacementMode
    target_path: Path
    source_fingerprint: Fingerprint
    target_fingerprint: Fingerprint
    failure_code: str | None = None


class StoryArcPlacementJournal(Protocol):
    """Synchronous observation hook called around filesystem actions.

    This callback is not an async database durability boundary.  An async
    integration must persist and commit its prepared action before dispatching
    this service to a worker thread; these events then provide reconciliation
    evidence around the actual publish/remove call.
    """

    def __call__(self, event: StoryArcPlacementJournalEvent) -> None: ...


class CancellationRequested(Protocol):
    """Cheap bounded cancellation predicate used between copy chunks."""

    def __call__(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class StoryArcPlacementRemovalResult:
    """Truthful result for an ownership-validated removal."""

    placement_path: Path
    removed: bool


@dataclass(frozen=True, slots=True)
class _RootGuard:
    """Pinned destination-root identity used across the publish boundary."""

    path: Path
    resolved_path: Path
    device: int
    inode: int


@dataclass(slots=True)
class _SecureParentDirectory:
    """Pinned no-follow directory handles for one target parent."""

    root_guard: _RootGuard
    path: Path
    root_fd: int
    parent_fd: int
    device: int
    inode: int


def prepare_story_arc_placement(
    plan: StoryArcPlacementPlan,
    *,
    existing_managed: ManagedStoryArcPlacementEvidence | None = None,
) -> StoryArcPlacementPreparation:
    """Capture immutable source/root/path evidence before a managed publish.

    This performs read-only filesystem validation.  Database adapters should
    call it only after closing their read transaction, then commit the returned
    evidence before dispatching :func:`execute_story_arc_placement`.
    """
    preparation, preview, source_path = _inspect_story_arc_placement(plan)
    if existing_managed is not None:
        existing_result = _resolve_existing_target(
            plan=plan,
            mode=preparation.mode,
            symlink_style=preparation.symlink_style,
            source_path=source_path,
            source_fingerprint=preparation.source_fingerprint,
            target_path=preparation.target_path,
            existing_managed=existing_managed,
        )
        if existing_result is not None:
            return preparation
    elif preview.state is StoryArcPlacementPreviewState.BLOCKED:
        # Preview is intentionally cheap and does not hash arbitrary existing
        # files.  Preparation performs the bounded comparison so callers get
        # the truthful identical-user versus different-content classification
        # before any ownership row is reserved.
        _resolve_existing_target(
            plan=plan,
            mode=preparation.mode,
            symlink_style=preparation.symlink_style,
            source_path=source_path,
            source_fingerprint=preparation.source_fingerprint,
            target_path=preparation.target_path,
            existing_managed=None,
        )
        _raise_from_preview(preview)
    if preview.state is not StoryArcPlacementPreviewState.READY:
        raise StoryArcPlacementSafetyError(
            "preview_not_ready",
            preview.reason or "Story-arc placement is not ready for preparation",
        )
    return preparation


def recover_prepared_story_arc_placement(
    plan: StoryArcPlacementPlan,
    evidence: PreparedManagedStoryArcPlacementEvidence,
) -> StoryArcPlacementResult | None:
    """Recover a target published after prepare but before DB checkpoint.

    No artifact is created or changed here.  The target is accepted only when
    every precommitted ownership field still matches and its representation and
    content exactly match the canonical source.  A missing target means the
    prepared operation may be retried normally.
    """
    preparation, _preview, source_path = _inspect_story_arc_placement(plan)
    mode = _coerce_mode(evidence.mode)
    symlink_style = _coerce_symlink_style(evidence.symlink_style)
    if not evidence.operation_token:
        raise StoryArcPlacementOwnershipError(
            "operation_token_missing",
            "Prepared story-arc placement has no ownership operation token",
        )
    if (
        evidence.issue_story_arc_id != preparation.issue_story_arc_id
        or Path(evidence.placement_path) != preparation.target_path
        or mode is not preparation.mode
        or symlink_style is not preparation.symlink_style
        or evidence.source_fingerprint != preparation.source_fingerprint
        or evidence.destination_root_fingerprint != preparation.destination_root_fingerprint
    ):
        raise StoryArcPlacementOwnershipError(
            "prepared_evidence_mismatch",
            "Prepared story-arc ownership evidence no longer matches the requested placement",
        )
    target_path = preparation.target_path
    if not _path_exists(target_path):
        return None
    _validate_existing_representation(mode, symlink_style, source_path, target_path)
    if not _target_content_matches_source(
        target_path,
        source_path,
        preparation.source_fingerprint,
    ):
        raise StoryArcPlacementSafetyError(
            "prepared_target_mismatch",
            "Prepared story-arc target does not match the canonical source",
        )
    # The database token is not physically bound to the artifact.  A restart
    # therefore cannot prove whether Pullbox or another process created an
    # identical target after preparation.  Preserve the file and track it as
    # referenced; only an uninterrupted publish may establish managed ownership.
    return StoryArcPlacementResult(
        issue_story_arc_id=preparation.issue_story_arc_id,
        library_file_id=plan.library_file_id,
        state=StoryArcPlacementResultState.REFERENCED_EXISTING,
        target_path=target_path,
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
        symlink_style=None,
        rendered_reading_order=preparation.rendered_reading_order,
        source_fingerprint=dict(preparation.source_fingerprint),
        target_fingerprint=_fingerprint_target(target_path),
    )


def inspect_story_arc_placement(
    plan: StoryArcPlacementPlan,
    *,
    existing: StoryArcPlacementInspectionEvidence | None = None,
) -> StoryArcPlacementInspection:
    """Inspect one target without creating, replacing, repairing, or deleting it.

    Traversal and hashing use the same no-follow, root-anchored boundary as
    execution.  Expected drift is represented as data so API/UI callers do not
    need to duplicate filesystem logic or turn ordinary lifecycle states into
    exceptions.
    """
    mode = _coerce_mode(plan.mode)
    symlink_style = _coerce_symlink_style(plan.symlink_style)
    _validate_mode_and_style(mode, symlink_style)
    preview = preview_story_arc_placement(
        canonical_path=plan.canonical_path,
        destination_root=plan.destination_root,
        values=plan.values,
        mode=mode,
        symlink_style=symlink_style,
        folder_template=plan.folder_template,
        file_template=plan.file_template,
    )
    ownership = _inspection_ownership(existing)
    if existing is not None and ownership is None:
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=Path(existing.placement_path),
            code="ownership_invalid",
            reason="Placement ownership evidence is invalid",
        )
    if mode is StoryArcPlacementMode.REFERENCE_ONLY and existing is None:
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.FREE,
            target_path=None,
            proposed_ownership="referenced",
        )

    rendered_target = preview.target_path
    if mode is not StoryArcPlacementMode.REFERENCE_ONLY and rendered_target is None:
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=None,
            code=preview.collision.value,
            reason=preview.reason,
        )
    target_path = (
        Path(existing.placement_path)
        if mode is StoryArcPlacementMode.REFERENCE_ONLY and existing is not None
        else rendered_target
    )
    if target_path is None:  # pragma: no cover - exhaustiveness above
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=None,
            code="target_unavailable",
            reason="Story-arc placement has no inspectable target",
        )
    if existing is not None and Path(existing.placement_path) != target_path:
        return _tracked_inspection_result(
            preview,
            ownership=ownership,
            target_path=target_path,
            code="destination_mismatch",
            reason="Recorded placement belongs to another destination",
        )
    if preview.state is StoryArcPlacementPreviewState.BLOCKED and preview.collision not in {
        StoryArcCollisionKind.DIFFERENT_CONTENT,
        StoryArcCollisionKind.CASE_ONLY,
    }:
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=target_path,
            code=preview.collision.value,
            reason=preview.reason,
        )
    if preview.collision is StoryArcCollisionKind.CASE_ONLY:
        if existing is not None:
            return _tracked_inspection_result(
                preview,
                ownership=ownership,
                target_path=target_path,
                code="case_only",
                reason=preview.reason,
                collision=StoryArcCollisionKind.CASE_ONLY,
            )
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=target_path,
            code="case_only",
            reason=preview.reason,
            collision=StoryArcCollisionKind.CASE_ONLY,
        )

    try:
        source_path = _validated_regular_source(plan.canonical_path)
        source_fingerprint = _fingerprint_regular_nofollow(source_path)
        root_guard = _validated_root(plan.destination_root)
        _validate_target_lexically(root_guard.path, target_path)
        _validate_path_limits(target_path)
        _reject_canonical_destination(source_path, target_path)
        try:
            parent_context = _open_secure_parent_directory(
                root_guard,
                target_path.parent,
                create=False,
            )
            with parent_context as secure_parent:
                _assert_parent_path_stable(secure_parent)
                if not _entry_exists_at(secure_parent.parent_fd, target_path.name):
                    return _missing_inspection_result(
                        preview,
                        ownership=ownership,
                        target_path=target_path,
                        source_fingerprint=source_fingerprint,
                    )
                target_fingerprint = _fingerprint_target_at(
                    secure_parent,
                    target_path.name,
                    canonical_path=source_path,
                )
                _assert_parent_path_stable(secure_parent)
                if existing is None:
                    state = (
                        StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL
                        if _fingerprint_content_matches(
                            target_fingerprint,
                            source_fingerprint,
                        )
                        else StoryArcPlacementInspectionState.DIFFERENT_CONTENT
                    )
                    return _inspection_result(
                        preview,
                        state=state,
                        target_path=target_path,
                        code=(
                            "untracked_identical"
                            if state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL
                            else "different_content"
                        ),
                        reason=(
                            "An identical untracked artifact already exists"
                            if state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL
                            else "A different artifact exists at the placement destination"
                        ),
                        source_fingerprint=source_fingerprint,
                        target_fingerprint=target_fingerprint,
                    )
                drift_code = _tracked_drift_code(
                    plan=plan,
                    evidence=existing,
                    ownership=ownership,
                    source_path=source_path,
                    source_fingerprint=source_fingerprint,
                    target_fingerprint=target_fingerprint,
                    target_name=target_path.name,
                    parent=secure_parent,
                )
                if drift_code is not None:
                    return _tracked_inspection_result(
                        preview,
                        ownership=ownership,
                        target_path=target_path,
                        code=drift_code,
                        reason=_inspection_drift_reason(drift_code),
                        source_fingerprint=source_fingerprint,
                        target_fingerprint=target_fingerprint,
                    )
                return _inspection_result(
                    preview,
                    state=(
                        StoryArcPlacementInspectionState.MANAGED_CURRENT
                        if ownership is StoryArcPlacementOwnership.MANAGED
                        else StoryArcPlacementInspectionState.REFERENCED_CURRENT
                    ),
                    target_path=target_path,
                    source_fingerprint=source_fingerprint,
                    target_fingerprint=target_fingerprint,
                    proposed_ownership=("managed" if ownership is None else ownership.value),
                )
        except FileNotFoundError:
            return _missing_inspection_result(
                preview,
                ownership=ownership,
                target_path=target_path,
                source_fingerprint=source_fingerprint,
            )
    except StoryArcPlacementError as exc:
        if existing is not None and ownership is not None:
            return _tracked_inspection_result(
                preview,
                ownership=ownership,
                target_path=target_path,
                code=exc.code,
                reason=str(exc),
            )
        if exc.code in {
            "dangling_symlink",
            "fingerprint_mismatch",
            "not_regular_file",
        }:
            return _inspection_result(
                preview,
                state=StoryArcPlacementInspectionState.DIFFERENT_CONTENT,
                target_path=target_path,
                code="different_content",
                reason="A different artifact exists at the placement destination",
                collision=StoryArcCollisionKind.DIFFERENT_CONTENT,
            )
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=target_path,
            code=exc.code,
            reason=str(exc),
            collision=_collision_from_code(exc.code),
        )
    except OSError:
        if existing is not None and ownership is not None:
            return _tracked_inspection_result(
                preview,
                ownership=ownership,
                target_path=target_path,
                code="inspection_failed",
                reason="Placement could not be inspected safely",
            )
        return _inspection_result(
            preview,
            state=StoryArcPlacementInspectionState.BLOCKED,
            target_path=target_path,
            code="inspection_failed",
            reason="Placement could not be inspected safely",
        )


def _inspection_result(
    preview: StoryArcPlacementPreview,
    *,
    state: StoryArcPlacementInspectionState,
    target_path: Path | None,
    code: str | None = None,
    reason: str | None = None,
    collision: StoryArcCollisionKind | None = None,
    proposed_ownership: str | None = None,
    source_fingerprint: Fingerprint | None = None,
    target_fingerprint: Fingerprint | None = None,
) -> StoryArcPlacementInspection:
    effective_collision = (
        collision
        if collision is not None
        else (
            preview.collision
            if state
            in {
                StoryArcPlacementInspectionState.BLOCKED,
                StoryArcPlacementInspectionState.DIFFERENT_CONTENT,
            }
            else StoryArcCollisionKind.NONE
        )
    )
    source_size = source_fingerprint.get("size") if source_fingerprint is not None else None
    required_bytes = preview.required_bytes
    if (
        required_bytes == 0
        and preview.mode is StoryArcPlacementMode.COPY
        and isinstance(source_size, int)
    ):
        required_bytes = source_size
    return StoryArcPlacementInspection(
        state=state,
        mode=preview.mode,
        target_path=target_path,
        collision=effective_collision,
        code=code,
        reason=reason,
        required_bytes=required_bytes,
        proposed_ownership=(
            preview.proposed_ownership if proposed_ownership is None else proposed_ownership
        ),
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
    )


def _inspection_ownership(
    evidence: StoryArcPlacementInspectionEvidence | None,
) -> StoryArcPlacementOwnership | None:
    if evidence is None:
        return None
    try:
        return StoryArcPlacementOwnership(evidence.ownership)
    except ValueError:
        return None


def _tracked_inspection_result(
    preview: StoryArcPlacementPreview,
    *,
    ownership: StoryArcPlacementOwnership | None,
    target_path: Path,
    code: str,
    reason: str | None,
    collision: StoryArcCollisionKind | None = None,
    source_fingerprint: Fingerprint | None = None,
    target_fingerprint: Fingerprint | None = None,
) -> StoryArcPlacementInspection:
    managed = ownership is StoryArcPlacementOwnership.MANAGED
    return _inspection_result(
        preview,
        state=(
            StoryArcPlacementInspectionState.MANAGED_DRIFTED
            if managed
            else StoryArcPlacementInspectionState.REFERENCED_DRIFTED
        ),
        target_path=target_path,
        code=code,
        reason=reason,
        collision=collision,
        proposed_ownership="managed" if managed else "referenced",
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
    )


def _missing_inspection_result(
    preview: StoryArcPlacementPreview,
    *,
    ownership: StoryArcPlacementOwnership | None,
    target_path: Path,
    source_fingerprint: Fingerprint,
) -> StoryArcPlacementInspection:
    if ownership is StoryArcPlacementOwnership.MANAGED:
        state = StoryArcPlacementInspectionState.MANAGED_MISSING
    elif ownership is StoryArcPlacementOwnership.REFERENCED:
        state = StoryArcPlacementInspectionState.REFERENCED_MISSING
    else:
        state = StoryArcPlacementInspectionState.FREE
    return _inspection_result(
        preview,
        state=state,
        target_path=target_path,
        code=None if state is StoryArcPlacementInspectionState.FREE else "target_missing",
        reason=None if state is StoryArcPlacementInspectionState.FREE else "Placement is missing",
        source_fingerprint=source_fingerprint,
        proposed_ownership=(
            "managed"
            if ownership is None or ownership is StoryArcPlacementOwnership.MANAGED
            else "referenced"
        ),
    )


def _tracked_drift_code(
    *,
    plan: StoryArcPlacementPlan,
    evidence: StoryArcPlacementInspectionEvidence,
    ownership: StoryArcPlacementOwnership | None,
    source_path: Path,
    source_fingerprint: Fingerprint,
    target_fingerprint: Fingerprint,
    target_name: str,
    parent: _SecureParentDirectory,
) -> str | None:
    if evidence.source_fingerprint and evidence.source_fingerprint != source_fingerprint:
        return "source_fingerprint_mismatch"
    if evidence.target_fingerprint and evidence.target_fingerprint != target_fingerprint:
        if ownership is StoryArcPlacementOwnership.MANAGED:
            try:
                expected_mode = _coerce_mode(evidence.mode)
                expected_style = _coerce_symlink_style(evidence.symlink_style)
                _validate_mode_and_style(expected_mode, expected_style)
                _validate_existing_representation_at(
                    expected_mode,
                    expected_style,
                    source_path,
                    source_fingerprint,
                    target_name,
                    parent,
                )
            except (ValueError, StoryArcPlacementError):
                return "representation_changed"
        return "target_fingerprint_mismatch"
    if not _fingerprint_content_matches(target_fingerprint, source_fingerprint):
        return "content_changed"
    if ownership is StoryArcPlacementOwnership.MANAGED:
        try:
            expected_mode = _coerce_mode(evidence.mode)
            expected_style = _coerce_symlink_style(evidence.symlink_style)
            requested_style = _coerce_symlink_style(plan.symlink_style)
            _validate_mode_and_style(expected_mode, expected_style)
            if (
                expected_mode is not _coerce_mode(plan.mode)
                or expected_style is not requested_style
            ):
                return "representation_changed"
            _validate_existing_representation_at(
                expected_mode,
                expected_style,
                source_path,
                source_fingerprint,
                target_name,
                parent,
            )
        except (ValueError, StoryArcPlacementError):
            return "representation_changed"
    return None


def _fingerprint_content_matches(
    target_fingerprint: Fingerprint,
    source_fingerprint: Fingerprint,
) -> bool:
    content = (
        target_fingerprint.get("content")
        if target_fingerprint.get("kind") == "symlink"
        else target_fingerprint
    )
    return isinstance(content, dict) and content.get("sha256") == source_fingerprint.get("sha256")


def _inspection_drift_reason(code: str) -> str:
    return {
        "source_fingerprint_mismatch": "Canonical source changed after placement was recorded",
        "target_fingerprint_mismatch": "Placement changed after it was recorded",
        "representation_changed": "Placement representation changed after it was recorded",
        "content_changed": "Placement content no longer matches its canonical source",
    }.get(code, "Placement no longer matches its recorded evidence")


def _collision_from_code(code: str) -> StoryArcCollisionKind:
    try:
        return StoryArcCollisionKind(code)
    except ValueError:
        return StoryArcCollisionKind.NONE


def _inspect_story_arc_placement(
    plan: StoryArcPlacementPlan,
) -> tuple[StoryArcPlacementPreparation, StoryArcPlacementPreview, Path]:
    mode = _coerce_mode(plan.mode)
    symlink_style = _coerce_symlink_style(plan.symlink_style)
    _validate_mode_and_style(mode, symlink_style)
    if mode is StoryArcPlacementMode.REFERENCE_ONLY:
        raise StoryArcPlacementSafetyError(
            "managed_preparation_required",
            "Reference-only placement does not use managed publish preparation",
        )
    if plan.issue_story_arc_id <= 0:
        raise ValueError("Story-arc membership id must be positive")
    preview = preview_story_arc_placement(
        canonical_path=plan.canonical_path,
        destination_root=plan.destination_root,
        values=plan.values,
        mode=mode,
        symlink_style=symlink_style,
        folder_template=plan.folder_template,
        file_template=plan.file_template,
    )
    target_path = preview.target_path
    if target_path is None:
        _raise_from_preview(preview)
        raise StoryArcPlacementSafetyError(
            "target_unavailable",
            "Story-arc placement preview did not produce a destination",
        )
    source_path = _validated_regular_source(plan.canonical_path)
    root_guard = _validated_root(plan.destination_root)
    _validate_target_lexically(root_guard.path, target_path)
    _validate_safe_existing_parents(root_guard, target_path.parent)
    _validate_path_limits(target_path)
    _reject_canonical_destination(source_path, target_path)
    return (
        StoryArcPlacementPreparation(
            issue_story_arc_id=plan.issue_story_arc_id,
            target_path=target_path,
            mode=mode,
            symlink_style=symlink_style,
            rendered_reading_order=plan.values.reading_order,
            source_fingerprint=_fingerprint_regular_nofollow(source_path),
            destination_root_fingerprint=_root_fingerprint(root_guard),
        ),
        preview,
        source_path,
    )


def execute_story_arc_placement(
    plan: StoryArcPlacementPlan,
    *,
    existing_managed: ManagedStoryArcPlacementEvidence | None = None,
    preparation: StoryArcPlacementPreparation | None = None,
    cancellation_requested: CancellationRequested | None = None,
    journal: StoryArcPlacementJournal | None = None,
) -> StoryArcPlacementResult:
    """Execute one resolved membership placement without moving its source."""
    mode = _coerce_mode(plan.mode)
    symlink_style = _coerce_symlink_style(plan.symlink_style)
    _validate_mode_and_style(mode, symlink_style)
    if plan.issue_story_arc_id <= 0:
        raise ValueError("Story-arc membership id must be positive")

    if mode is StoryArcPlacementMode.REFERENCE_ONLY:
        if existing_managed is not None:
            raise StoryArcPlacementOwnershipError(
                "mode_changed",
                "Managed placement evidence cannot be applied to reference-only mode",
            )
        return StoryArcPlacementResult(
            issue_story_arc_id=plan.issue_story_arc_id,
            library_file_id=plan.library_file_id,
            state=StoryArcPlacementResultState.REFERENCE_ONLY,
            target_path=None,
            mode=mode,
            ownership=StoryArcPlacementOwnership.REFERENCED,
            symlink_style=None,
            rendered_reading_order=plan.values.reading_order,
            source_fingerprint={},
            target_fingerprint={},
        )

    _raise_if_cancelled(cancellation_requested)
    preview = preview_story_arc_placement(
        canonical_path=plan.canonical_path,
        destination_root=plan.destination_root,
        values=plan.values,
        mode=mode,
        symlink_style=symlink_style,
        folder_template=plan.folder_template,
        file_template=plan.file_template,
    )
    target_path = preview.target_path
    if target_path is None:
        _raise_from_preview(preview)
        raise StoryArcPlacementSafetyError(
            "target_unavailable",
            "Story-arc placement preview did not produce a destination",
        )
    source_path = _validated_regular_source(plan.canonical_path)
    root_guard = _validated_root(plan.destination_root)
    _validate_target_lexically(root_guard.path, target_path)
    _validate_safe_existing_parents(root_guard, target_path.parent)
    _validate_path_limits(target_path)
    _reject_canonical_destination(source_path, target_path)

    source_fingerprint = _fingerprint_regular_nofollow(source_path)
    if preparation is not None:
        current_preparation = StoryArcPlacementPreparation(
            issue_story_arc_id=plan.issue_story_arc_id,
            target_path=target_path,
            mode=mode,
            symlink_style=symlink_style,
            rendered_reading_order=plan.values.reading_order,
            source_fingerprint=dict(source_fingerprint),
            destination_root_fingerprint=_root_fingerprint(root_guard),
        )
        if current_preparation != preparation:
            raise StoryArcPlacementOwnershipError(
                "prepared_evidence_mismatch",
                "Story-arc source, root, or target changed after durable preparation",
            )
    existing_result = _resolve_existing_target(
        plan=plan,
        mode=mode,
        symlink_style=symlink_style,
        source_path=source_path,
        source_fingerprint=source_fingerprint,
        target_path=target_path,
        existing_managed=existing_managed,
    )
    if existing_result is not None:
        return existing_result

    if preview.state is StoryArcPlacementPreviewState.BLOCKED:
        _raise_from_preview(preview)
    if preview.state is not StoryArcPlacementPreviewState.READY:
        raise StoryArcPlacementSafetyError(
            "preview_not_ready",
            preview.reason or "Story-arc placement is not ready for execution",
        )
    if existing_managed is not None:
        _validate_managed_evidence(
            existing_managed,
            plan=plan,
            mode=mode,
            symlink_style=symlink_style,
            target_path=target_path,
            source_fingerprint=source_fingerprint,
        )

    prepared_event = StoryArcPlacementJournalEvent(
        stage="prepared",
        operation="publish",
        issue_story_arc_id=plan.issue_story_arc_id,
        mode=mode,
        target_path=target_path,
        source_fingerprint=dict(source_fingerprint),
        target_fingerprint={},
    )
    _record_journal(journal, prepared_event)

    try:
        with _open_secure_parent_directory(
            root_guard,
            target_path.parent,
            create=True,
        ) as secure_parent:
            _raise_if_cancelled(cancellation_requested)
            _recheck_free_destination_at(secure_parent, target_path.name)
            if mode is StoryArcPlacementMode.COPY:
                created_identity = _publish_copy_at(
                    source_path,
                    target_path.name,
                    secure_parent,
                    source_fingerprint,
                    cancellation_requested,
                )
            elif mode is StoryArcPlacementMode.HARDLINK:
                created_identity = _publish_hardlink_at(
                    source_path,
                    target_path.name,
                    secure_parent,
                    source_fingerprint,
                )
            elif mode is StoryArcPlacementMode.SYMLINK:
                if symlink_style is None:  # pragma: no cover - validated above
                    raise StoryArcPlacementSafetyError(
                        "symlink_style_required",
                        "Symlink placement requires a style",
                    )
                created_identity = _publish_symlink_at(
                    source_path,
                    target_path.name,
                    target_path.parent,
                    secure_parent,
                    symlink_style,
                )
            else:  # pragma: no cover - enum exhaustiveness
                raise StoryArcPlacementSafetyError(
                    "unsupported_mode",
                    f"Unsupported story-arc placement mode: {mode.value}",
                )
            try:
                _assert_parent_path_stable(secure_parent)
                target_fingerprint = _validate_published_target_at(
                    mode,
                    symlink_style,
                    source_path,
                    source_fingerprint,
                    target_path.name,
                    secure_parent,
                )
                _fsync_directory(secure_parent.parent_fd)
                _assert_parent_path_stable(secure_parent)
            except BaseException:
                _remove_created_target_at(
                    secure_parent,
                    target_path.name,
                    expected_identity=created_identity,
                )
                raise
    except StoryArcPlacementError as exc:
        _record_failure(journal, prepared_event, exc.code)
        raise
    except OSError as exc:
        error = _categorized_os_error(mode, exc)
        _record_failure(journal, prepared_event, error.code)
        raise error from exc

    result = StoryArcPlacementResult(
        issue_story_arc_id=plan.issue_story_arc_id,
        library_file_id=plan.library_file_id,
        state=StoryArcPlacementResultState.CREATED,
        target_path=target_path,
        mode=mode,
        ownership=StoryArcPlacementOwnership.MANAGED,
        symlink_style=symlink_style,
        rendered_reading_order=plan.values.reading_order,
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
    )
    _record_journal(
        journal,
        StoryArcPlacementJournalEvent(
            stage="published",
            operation="publish",
            issue_story_arc_id=plan.issue_story_arc_id,
            mode=mode,
            target_path=target_path,
            source_fingerprint=dict(source_fingerprint),
            target_fingerprint=dict(target_fingerprint),
        ),
    )
    return result


def execute_story_arc_placement_batch(
    plans: Sequence[StoryArcPlacementPlan],
    *,
    cancellation_requested: CancellationRequested | None = None,
    journal: StoryArcPlacementJournal | None = None,
) -> tuple[StoryArcPlacementResult, ...]:
    """Execute a caller-bounded batch with cancellation between memberships."""
    if len(plans) > MAX_STORY_ARC_PLACEMENTS_PER_BATCH:
        raise ValueError(
            "Story-arc placement batch exceeds the bounded execution limit of "
            f"{MAX_STORY_ARC_PLACEMENTS_PER_BATCH}"
        )
    results: list[StoryArcPlacementResult] = []
    for plan in plans:
        _raise_if_cancelled(cancellation_requested)
        results.append(
            execute_story_arc_placement(
                plan,
                cancellation_requested=cancellation_requested,
                journal=journal,
            )
        )
    return tuple(results)


def repair_managed_story_arc_placement(
    plan: StoryArcPlacementPlan,
    evidence: ManagedStoryArcPlacementEvidence,
    *,
    cancellation_requested: CancellationRequested | None = None,
    journal: StoryArcPlacementJournal | None = None,
) -> StoryArcPlacementResult:
    """Idempotently recreate a missing managed artifact after evidence checks."""
    _require_managed_ownership(evidence)
    return execute_story_arc_placement(
        plan,
        existing_managed=evidence,
        cancellation_requested=cancellation_requested,
        journal=journal,
    )


def remove_managed_story_arc_placement(
    evidence: ManagedStoryArcPlacementEvidence,
    *,
    destination_root: Path,
    canonical_path: Path | None,
    journal: StoryArcPlacementJournal | None = None,
) -> StoryArcPlacementRemovalResult:
    """Remove only an unchanged, action-owned managed placement artifact."""
    mode, _ownership, _symlink_style = _require_managed_ownership(evidence)
    root_guard = _validated_root(destination_root)
    target_path = Path(evidence.placement_path)
    _validate_target_lexically(root_guard.path, target_path)
    if canonical_path is not None:
        _reject_canonical_destination(canonical_path.resolve(strict=False), target_path)

    prepared: StoryArcPlacementJournalEvent | None = None
    try:
        with _open_secure_parent_directory(
            root_guard,
            target_path.parent,
            create=False,
        ) as secure_parent:
            if not _entry_exists_at(secure_parent.parent_fd, target_path.name):
                return StoryArcPlacementRemovalResult(target_path, removed=False)
            actual_fingerprint = _fingerprint_target_at(
                secure_parent,
                target_path.name,
                canonical_path=canonical_path,
            )
            if actual_fingerprint != evidence.target_fingerprint:
                raise StoryArcPlacementSafetyError(
                    "fingerprint_mismatch",
                    "Managed story-arc placement changed after it was recorded",
                )
            _validate_removal_representation_at(
                mode,
                secure_parent,
                target_path.name,
            )

            prepared = StoryArcPlacementJournalEvent(
                stage="remove_prepared",
                operation="remove",
                issue_story_arc_id=evidence.issue_story_arc_id,
                mode=mode,
                target_path=target_path,
                source_fingerprint=dict(evidence.source_fingerprint),
                target_fingerprint=dict(evidence.target_fingerprint),
            )
            _record_journal(journal, prepared)
            _assert_parent_path_stable(secure_parent)
            if (
                not _entry_exists_at(secure_parent.parent_fd, target_path.name)
                or _fingerprint_target_at(
                    secure_parent,
                    target_path.name,
                    canonical_path=canonical_path,
                )
                != evidence.target_fingerprint
            ):
                raise StoryArcPlacementSafetyError(
                    "fingerprint_mismatch",
                    "Managed story-arc placement changed before removal",
                )
            os.unlink(target_path.name, dir_fd=secure_parent.parent_fd)
            _fsync_directory(secure_parent.parent_fd)
            _assert_parent_path_stable(secure_parent)
    except StoryArcPlacementError as exc:
        if prepared is not None:
            _record_failure(journal, prepared, exc.code)
        raise
    except OSError as exc:
        error = _categorized_os_error(mode, exc)
        if prepared is not None:
            _record_failure(journal, prepared, error.code)
        raise error from exc
    _record_journal(
        journal,
        StoryArcPlacementJournalEvent(
            stage="removed",
            operation="remove",
            issue_story_arc_id=evidence.issue_story_arc_id,
            mode=mode,
            target_path=target_path,
            source_fingerprint=dict(evidence.source_fingerprint),
            target_fingerprint=dict(evidence.target_fingerprint),
        ),
    )
    return StoryArcPlacementRemovalResult(target_path, removed=True)


def _coerce_mode(value: StoryArcPlacementMode | str) -> StoryArcPlacementMode:
    try:
        return StoryArcPlacementMode(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported story-arc placement mode: {value}") from exc


def _coerce_symlink_style(
    value: StoryArcSymlinkStyle | str | None,
) -> StoryArcSymlinkStyle | None:
    if value is None:
        return None
    try:
        return StoryArcSymlinkStyle(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported story-arc symlink style: {value}") from exc


def _validate_mode_and_style(
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
) -> None:
    if mode is StoryArcPlacementMode.SYMLINK and symlink_style is None:
        raise ValueError("Story-arc symlink mode requires a symlink style")
    if mode is not StoryArcPlacementMode.SYMLINK and symlink_style is not None:
        raise ValueError("Story-arc symlink style is only valid for symlink mode")


def _validated_regular_source(source_path: Path | None) -> Path:
    if source_path is None or not source_path.is_absolute():
        raise StoryArcPlacementSafetyError(
            "source_unavailable",
            "Canonical story-arc source must be an absolute path",
        )
    if source_path.is_symlink():
        raise StoryArcPlacementSafetyError(
            "source_symlink",
            "Canonical story-arc source cannot be a symbolic link",
        )
    try:
        resolved = source_path.resolve(strict=True)
        source_stat = resolved.stat()
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "source_unavailable",
            "Canonical story-arc source is unavailable",
        ) from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise StoryArcPlacementSafetyError(
            "source_unavailable",
            "Canonical story-arc source is not a regular file",
        )
    return resolved


def _validated_root(root: Path | None) -> _RootGuard:
    if root is None or not root.is_absolute():
        raise StoryArcPlacementSafetyError(
            "root_unavailable",
            "Story-arc destination root must be an absolute path",
        )
    if root.is_symlink():
        raise StoryArcPlacementSafetyError(
            "symlink_root",
            "Story-arc destination root cannot be a symbolic link",
        )
    try:
        resolved = root.resolve(strict=True)
        root_stat = root.stat()
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "root_unavailable",
            "Story-arc destination root is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise StoryArcPlacementSafetyError(
            "root_unavailable",
            "Story-arc destination root is not a directory",
        )
    return _RootGuard(
        path=root,
        resolved_path=resolved,
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
    )


def _root_fingerprint(root_guard: _RootGuard) -> Fingerprint:
    return {
        "schema_version": 1,
        "kind": "directory",
        "path": str(root_guard.path),
        "resolved_path": str(root_guard.resolved_path),
        "device": root_guard.device,
        "inode": root_guard.inode,
    }


def _secure_dir_fd_supported() -> bool:
    """Return whether this runtime can anchor every mutation to a directory fd."""
    return _SECURE_DIR_FD_SUPPORTED


def _require_secure_dir_fd_support() -> None:
    if not _secure_dir_fd_supported():
        raise StoryArcPlacementSafetyError(
            "secure_dir_fd_unavailable",
            "This platform cannot safely anchor story-arc filesystem operations",
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_component(name: str, *, parent_fd: int) -> int:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise StoryArcPlacementSafetyError(
                "symlink_parent",
                "Story-arc destination has a symbolic-link parent",
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise StoryArcPlacementSafetyError(
                "parent_not_directory",
                "Story-arc destination parent is not a directory",
            ) from exc
        raise
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise StoryArcPlacementSafetyError(
            "parent_not_directory",
            "Story-arc destination parent is not a directory",
        )
    return descriptor


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "durability_unavailable",
            "Story-arc directory changes could not be durably synchronized",
        ) from exc


@contextmanager
def _open_secure_parent_directory(
    root_guard: _RootGuard,
    parent: Path,
    *,
    create: bool,
) -> Iterator[_SecureParentDirectory]:
    """Open a target parent one no-follow component at a time.

    All later entry operations use the returned ``parent_fd``.  Renaming or
    replacing any pathname component therefore cannot redirect a publish or
    removal outside the directory that was actually inspected.
    """
    _require_secure_dir_fd_support()
    relative = parent.relative_to(root_guard.path)
    try:
        root_fd = os.open(root_guard.path, _directory_open_flags())
    except OSError as exc:
        code = "symlink_root" if exc.errno in {errno.ELOOP, errno.EMLINK} else "root_changed"
        raise StoryArcPlacementSafetyError(
            code,
            "Story-arc destination root changed during execution",
        ) from exc
    current_fd = root_fd
    try:
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_dev != root_guard.device
            or root_stat.st_ino != root_guard.inode
        ):
            raise StoryArcPlacementSafetyError(
                "root_changed",
                "Story-arc destination root changed during execution",
            )
        for part in relative.parts:
            created = False
            try:
                child_fd = _open_directory_component(part, parent_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
                child_fd = _open_directory_component(part, parent_fd=current_fd)
            try:
                if created:
                    _fsync_directory(child_fd)
                    _fsync_directory(current_fd)
            except BaseException:
                os.close(child_fd)
                raise
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = child_fd
        parent_stat = os.fstat(current_fd)
        yield _SecureParentDirectory(
            root_guard=root_guard,
            path=parent,
            root_fd=root_fd,
            parent_fd=current_fd,
            device=parent_stat.st_dev,
            inode=parent_stat.st_ino,
        )
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _assert_parent_path_stable(parent: _SecureParentDirectory) -> None:
    """Fail when the configured path no longer identifies the pinned parent."""
    _recheck_root(parent.root_guard)
    try:
        with _open_secure_parent_directory(
            parent.root_guard,
            parent.path,
            create=False,
        ) as reopened:
            if reopened.device != parent.device or reopened.inode != parent.inode:
                raise StoryArcPlacementSafetyError(
                    "parent_changed",
                    "Story-arc destination parent changed during execution",
                )
    except StoryArcPlacementSafetyError as exc:
        if exc.code in {"root_changed", "symlink_root"}:
            raise
        raise StoryArcPlacementSafetyError(
            "parent_changed",
            "Story-arc destination parent changed during execution",
        ) from exc
    except FileNotFoundError as exc:
        raise StoryArcPlacementSafetyError(
            "parent_changed",
            "Story-arc destination parent changed during execution",
        ) from exc


def _recheck_root(root_guard: _RootGuard) -> None:
    root = root_guard.path
    if root.is_symlink():
        raise StoryArcPlacementSafetyError(
            "symlink_root",
            "Story-arc destination root became a symbolic link",
        )
    try:
        current_stat = root.stat()
        current_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "root_changed",
            "Story-arc destination root changed during execution",
        ) from exc
    if (
        not stat.S_ISDIR(current_stat.st_mode)
        or current_stat.st_dev != root_guard.device
        or current_stat.st_ino != root_guard.inode
        or current_resolved != root_guard.resolved_path
    ):
        raise StoryArcPlacementSafetyError(
            "root_changed",
            "Story-arc destination root changed during execution",
        )


def _validate_target_lexically(root: Path, target_path: Path) -> None:
    try:
        relative = target_path.relative_to(root)
    except ValueError as exc:
        raise StoryArcPlacementSafetyError(
            "path_escape",
            "Story-arc destination is outside the selected root",
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise StoryArcPlacementSafetyError(
            "path_escape",
            "Story-arc destination contains an unsafe path component",
        )


def _validate_safe_existing_parents(root_guard: _RootGuard, parent: Path) -> None:
    """Reject every existing symlink parent and every realpath escape."""
    _recheck_root(root_guard)
    root = root_guard.path
    relative = parent.relative_to(root)
    resolved_root = root_guard.resolved_path
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            try:
                resolved = current.resolve(strict=True)
            except OSError as exc:
                raise StoryArcPlacementSafetyError(
                    "symlink_parent",
                    "Story-arc destination has an unsafe symbolic-link parent",
                ) from exc
            code = "path_escape" if not resolved.is_relative_to(resolved_root) else "symlink_parent"
            raise StoryArcPlacementSafetyError(
                code,
                "Story-arc destination has a symbolic-link parent",
            )
        if current.exists() and not current.is_dir():
            raise StoryArcPlacementSafetyError(
                "parent_not_directory",
                "Story-arc destination parent is not a directory",
            )
        if not current.exists():
            break
    _recheck_root(root_guard)


def _ensure_safe_destination_parent(root_guard: _RootGuard, parent: Path) -> None:
    _recheck_root(root_guard)
    root = root_guard.path
    relative = parent.relative_to(root)
    resolved_root = root_guard.resolved_path
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise StoryArcPlacementSafetyError(
                "symlink_parent",
                "Story-arc destination has a symbolic-link parent",
            )
        if current.exists():
            if not current.is_dir():
                raise StoryArcPlacementSafetyError(
                    "parent_not_directory",
                    "Story-arc destination parent is not a directory",
                )
        else:
            with suppress(FileExistsError):
                current.mkdir()
            if current.is_symlink() or not current.is_dir():
                raise StoryArcPlacementSafetyError(
                    "symlink_parent",
                    "Story-arc destination parent changed during creation",
                )
        resolved_current = current.resolve(strict=True)
        if not resolved_current.is_relative_to(resolved_root):
            raise StoryArcPlacementSafetyError(
                "path_escape",
                "Story-arc destination parent resolves outside the selected root",
            )
        _recheck_root(root_guard)


def _validate_path_limits(target_path: Path) -> None:
    if len(os.fsencode(target_path.name)) > 255:
        raise StoryArcPlacementSafetyError(
            "name_too_long",
            "Rendered story-arc filename exceeds the supported length",
        )
    if len(os.fsencode(target_path)) > 4096:
        raise StoryArcPlacementSafetyError(
            "path_too_long",
            "Rendered story-arc path exceeds the supported length",
        )
    if len(str(target_path)) > 1000:
        raise StoryArcPlacementSafetyError(
            "path_too_long",
            "Rendered story-arc path exceeds the database path limit",
        )


def _reject_canonical_destination(source_path: Path, target_path: Path) -> None:
    if os.path.normcase(os.path.abspath(source_path)) == os.path.normcase(
        os.path.abspath(target_path)
    ):
        raise StoryArcPlacementSafetyError(
            "canonical_destination",
            "Story-arc placement path cannot be the canonical source path",
        )


def _resolve_existing_target(
    *,
    plan: StoryArcPlacementPlan,
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
    source_path: Path,
    source_fingerprint: Fingerprint,
    target_path: Path,
    existing_managed: ManagedStoryArcPlacementEvidence | None,
) -> StoryArcPlacementResult | None:
    case_collision = _case_only_collision(target_path)
    if case_collision is not None:
        raise StoryArcPlacementCollisionError(
            "case_only",
            "A case-only story-arc destination collision exists",
        )
    if not _path_exists(target_path):
        return None

    target_fingerprint = _fingerprint_target(target_path)
    if existing_managed is not None:
        _validate_managed_evidence(
            existing_managed,
            plan=plan,
            mode=mode,
            symlink_style=symlink_style,
            target_path=target_path,
            source_fingerprint=source_fingerprint,
        )
        if target_fingerprint != existing_managed.target_fingerprint:
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc placement changed after it was recorded",
            )
        _validate_existing_representation(mode, symlink_style, source_path, target_path)
        return StoryArcPlacementResult(
            issue_story_arc_id=plan.issue_story_arc_id,
            library_file_id=plan.library_file_id,
            state=StoryArcPlacementResultState.IDEMPOTENT,
            target_path=target_path,
            mode=mode,
            ownership=StoryArcPlacementOwnership.MANAGED,
            symlink_style=symlink_style,
            rendered_reading_order=plan.values.reading_order,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
        )

    same_inode = False
    with suppress(OSError):
        same_inode = target_path.samefile(source_path)
    identical = same_inode or _target_content_matches_source(
        target_path,
        source_path,
        source_fingerprint,
    )
    if identical and plan.adopt_identical_existing:
        return StoryArcPlacementResult(
            issue_story_arc_id=plan.issue_story_arc_id,
            library_file_id=plan.library_file_id,
            state=StoryArcPlacementResultState.REFERENCED_EXISTING,
            target_path=target_path,
            mode=StoryArcPlacementMode.REFERENCE_ONLY,
            ownership=StoryArcPlacementOwnership.REFERENCED,
            symlink_style=None,
            rendered_reading_order=plan.values.reading_order,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
        )
    if identical:
        raise StoryArcPlacementCollisionError(
            "identical_unconfirmed",
            "An identical user artifact requires explicit referenced-placement confirmation",
        )
    raise StoryArcPlacementCollisionError(
        "different_content",
        "A different artifact already exists at the story-arc destination",
    )


def _validate_managed_evidence(
    evidence: ManagedStoryArcPlacementEvidence,
    *,
    plan: StoryArcPlacementPlan,
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
    target_path: Path,
    source_fingerprint: Fingerprint,
) -> None:
    evidence_mode, _ownership, evidence_style = _require_managed_ownership(evidence)
    if evidence.issue_story_arc_id != plan.issue_story_arc_id:
        raise StoryArcPlacementOwnershipError(
            "membership_mismatch",
            "Managed placement evidence belongs to another membership",
        )
    if Path(evidence.placement_path) != target_path:
        raise StoryArcPlacementOwnershipError(
            "destination_mismatch",
            "Managed placement evidence belongs to another destination",
        )
    if evidence_mode is not mode or evidence_style is not symlink_style:
        raise StoryArcPlacementOwnershipError(
            "mode_changed",
            "Managed placement evidence does not match the requested mode",
        )
    if evidence.source_fingerprint != source_fingerprint:
        raise StoryArcPlacementSafetyError(
            "source_fingerprint_mismatch",
            "Canonical source changed after the managed placement was recorded",
        )
    if not evidence.target_fingerprint:
        raise StoryArcPlacementOwnershipError(
            "target_fingerprint_missing",
            "Managed placement evidence has no target fingerprint",
        )


def _require_managed_ownership(
    evidence: ManagedStoryArcPlacementEvidence,
) -> tuple[StoryArcPlacementMode, StoryArcPlacementOwnership, StoryArcSymlinkStyle | None]:
    mode = _coerce_mode(evidence.mode)
    try:
        ownership = StoryArcPlacementOwnership(evidence.ownership)
    except ValueError as exc:
        raise StoryArcPlacementOwnershipError(
            "ownership_invalid",
            "Story-arc placement ownership evidence is invalid",
        ) from exc
    symlink_style = _coerce_symlink_style(evidence.symlink_style)
    _validate_mode_and_style(mode, symlink_style)
    if (
        ownership is not StoryArcPlacementOwnership.MANAGED
        or mode is StoryArcPlacementMode.REFERENCE_ONLY
        or evidence.creating_action_id is None
    ):
        raise StoryArcPlacementOwnershipError(
            "not_managed",
            "Only an action-owned managed placement may be repaired or removed",
        )
    return mode, ownership, symlink_style


def _validate_existing_representation(
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
    source_path: Path,
    target_path: Path,
) -> None:
    if mode is StoryArcPlacementMode.SYMLINK:
        if not target_path.is_symlink() or target_path.resolve(strict=True) != source_path:
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink no longer resolves to its canonical source",
            )
        link_target = os.readlink(target_path)
        if symlink_style is StoryArcSymlinkStyle.ABSOLUTE and not os.path.isabs(link_target):
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink style changed",
            )
        if symlink_style is StoryArcSymlinkStyle.RELATIVE and os.path.isabs(link_target):
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink style changed",
            )
        return
    if target_path.is_symlink() or not target_path.is_file():
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed story-arc artifact type changed",
        )
    if mode is StoryArcPlacementMode.HARDLINK and not target_path.samefile(source_path):
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed hardlink no longer references its canonical source",
        )


def _recheck_free_destination_at(
    parent: _SecureParentDirectory,
    target_name: str,
) -> None:
    if _case_only_collision_at(parent.parent_fd, target_name) is not None:
        raise StoryArcPlacementCollisionError(
            "case_only",
            "A case-only story-arc destination collision appeared during execution",
        )
    if _entry_exists_at(parent.parent_fd, target_name):
        raise StoryArcPlacementCollisionError(
            "destination_exists",
            "Story-arc destination appeared during execution; it was not overwritten",
        )


def _create_temporary_file_at(parent_fd: int) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(32):
        name = f"{_TEMP_PREFIX}{secrets.token_hex(12)}{_TEMP_SUFFIX}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_fd), name
        except FileExistsError:
            continue
    raise StoryArcPlacementSafetyError(
        "temporary_name_unavailable",
        "A unique story-arc temporary filename could not be allocated",
    )


def _publish_copy_at(
    source_path: Path,
    target_name: str,
    parent: _SecureParentDirectory,
    source_fingerprint: Fingerprint,
    cancellation_requested: CancellationRequested | None,
) -> _PublishedTarget:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source_path, source_flags)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Canonical source changed before story-arc copy",
        ) from exc
    temporary_fd = -1
    temporary_name = ""
    published = False
    published_stat: os.stat_result | None = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or not _stat_matches_fingerprint(
            source_stat,
            source_fingerprint,
        ):
            raise StoryArcPlacementSafetyError(
                "source_changed",
                "Canonical source changed before story-arc copy",
            )
        temporary_fd, temporary_name = _create_temporary_file_at(parent.parent_fd)
        digest = hashlib.sha256()
        with (
            os.fdopen(source_fd, "rb", closefd=False) as source,
            os.fdopen(temporary_fd, "wb", closefd=False) as target,
        ):
            while chunk := source.read(_COPY_CHUNK_BYTES):
                _raise_if_cancelled(cancellation_requested)
                target.write(chunk)
                digest.update(chunk)
            target.flush()
        os.fchmod(temporary_fd, stat.S_IMODE(source_stat.st_mode))
        os.utime(
            temporary_fd,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )
        os.fsync(temporary_fd)
        source_after = os.fstat(source_fd)
        temporary_stat = os.fstat(temporary_fd)
        if (
            temporary_stat.st_size != source_fingerprint.get("size")
            or digest.hexdigest() != source_fingerprint.get("sha256")
            or not _stat_matches_fingerprint(source_after, source_fingerprint)
        ):
            raise StoryArcPlacementSafetyError(
                "source_changed",
                "Canonical source changed during story-arc copy",
            )
        _raise_if_cancelled(cancellation_requested)
        try:
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=parent.parent_fd,
                dst_dir_fd=parent.parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise StoryArcPlacementCollisionError(
                "destination_exists",
                "Story-arc destination appeared before atomic copy publish",
            ) from exc
        published = True
    finally:
        try:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent.parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    if not published:
                        raise
            if published:
                # Removing the temporary hardlink changes ctime. Capture from
                # the still-open descriptor, never a potentially replaced path.
                published_stat = os.fstat(temporary_fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            os.close(source_fd)
    if published_stat is None:  # pragma: no cover - publish or raise
        raise StoryArcPlacementSafetyError(
            "publish_validation_failed",
            "Atomic story-arc copy publication produced no filesystem identity",
        )
    return _PublishedTarget(published_stat, sha256=digest.hexdigest())


def _publish_hardlink_at(
    source_path: Path,
    target_name: str,
    parent: _SecureParentDirectory,
    source_fingerprint: Fingerprint,
) -> _PublishedTarget:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source_path, flags)
    try:
        source_stat = os.fstat(descriptor)
        if not _stat_matches_fingerprint(source_stat, source_fingerprint):
            raise StoryArcPlacementSafetyError("source_changed", "Canonical source changed")
        if source_stat.st_dev != os.fstat(parent.parent_fd).st_dev:
            raise StoryArcPlacementSafetyError(
                "cross_device",
                "Hardlink source and story-arc destination are on different filesystems",
            )
        os.link(
            source_path,
            target_name,
            dst_dir_fd=parent.parent_fd,
            follow_symlinks=False,
        )
        return _PublishedTarget(os.fstat(descriptor), sha256=str(source_fingerprint["sha256"]))
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise StoryArcPlacementSafetyError(
                "cross_device",
                "Hardlink source and story-arc destination are on different filesystems",
            ) from exc
        raise
    finally:
        os.close(descriptor)


def _publish_symlink_at(
    source_path: Path,
    target_name: str,
    target_parent: Path,
    parent: _SecureParentDirectory,
    symlink_style: StoryArcSymlinkStyle,
) -> _PublishedTarget:
    link_target = (
        str(source_path)
        if symlink_style is StoryArcSymlinkStyle.ABSOLUTE
        else os.path.relpath(source_path, start=target_parent.resolve(strict=True))
    )
    prospective = Path(link_target)
    if not prospective.is_absolute():
        prospective = target_parent / prospective
    if prospective.resolve(strict=True) != source_path:
        raise StoryArcPlacementSafetyError(
            "symlink_target_mismatch",
            "Story-arc symlink would not resolve to the canonical source",
        )
    os.symlink(link_target, target_name, dir_fd=parent.parent_fd)
    created = _entry_lstat_at(parent.parent_fd, target_name)
    if not stat.S_ISLNK(created.st_mode):
        raise StoryArcPlacementSafetyError(
            "publish_validation_failed",
            "Published story-arc symlink changed before it could be validated",
        )
    return _PublishedTarget(created, link_target=link_target)


def _validate_published_target_at(
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
    source_path: Path,
    source_fingerprint: Fingerprint,
    target_name: str,
    parent: _SecureParentDirectory,
) -> Fingerprint:
    _validate_existing_representation_at(
        mode,
        symlink_style,
        source_path,
        source_fingerprint,
        target_name,
        parent,
    )
    target_fingerprint = _fingerprint_target_at(
        parent,
        target_name,
        canonical_path=source_path,
    )
    content = (
        target_fingerprint.get("content")
        if target_fingerprint.get("kind") == "symlink"
        else target_fingerprint
    )
    if not isinstance(content, dict) or content.get("sha256") != source_fingerprint.get("sha256"):
        raise StoryArcPlacementSafetyError(
            "publish_validation_failed",
            "Published story-arc artifact does not match its canonical source",
        )
    if mode is StoryArcPlacementMode.SYMLINK and content != source_fingerprint:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Canonical source changed while its story-arc symlink was published",
        )
    return target_fingerprint


def _remove_created_target_at(
    parent: _SecureParentDirectory,
    target_name: str,
    *,
    expected_identity: _PublishedTarget,
) -> None:
    try:
        current = _entry_lstat_at(parent.parent_fd, target_name)
        if _publication_stat_identity(current) != _publication_stat_identity(
            expected_identity.stat
        ):
            return
        if expected_identity.link_target is not None:
            if os.readlink(target_name, dir_fd=parent.parent_fd) != expected_identity.link_target:
                return
        elif (
            _fingerprint_regular_at(parent.parent_fd, target_name)["sha256"]
            != expected_identity.sha256
        ):
            return
        # Hashing may take time. Recheck metadata before removal, including
        # ctime so a recycled inode or an in-place edit does not prove ownership.
        current = _entry_lstat_at(parent.parent_fd, target_name)
        if _publication_stat_identity(current) != _publication_stat_identity(
            expected_identity.stat
        ):
            return
        os.unlink(target_name, dir_fd=parent.parent_fd)
        _fsync_directory(parent.parent_fd)
    except (OSError, StoryArcPlacementError):
        # If ownership cannot be proven, preserve the entry and original error.
        pass


def _publication_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stat_matches_fingerprint(current: os.stat_result, fingerprint: Fingerprint) -> bool:
    return (
        current.st_dev == fingerprint.get("device")
        and current.st_ino == fingerprint.get("inode")
        and current.st_size == fingerprint.get("size")
        and current.st_mtime_ns == fingerprint.get("mtime_ns")
    )


def _entry_lstat_at(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _entry_exists_at(parent_fd: int, name: str) -> bool:
    try:
        _entry_lstat_at(parent_fd, name)
    except FileNotFoundError:
        return False
    return True


def _fingerprint_regular_descriptor(descriptor: int) -> Fingerprint:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise StoryArcPlacementSafetyError(
            "not_regular_file",
            "Story-arc artifact is not a regular file",
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
        digest.update(chunk)
    after = os.fstat(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Story-arc artifact changed while it was fingerprinted",
        )
    return {
        "schema_version": 1,
        "kind": "regular",
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "device": after.st_dev,
        "inode": after.st_ino,
        "sha256": digest.hexdigest(),
    }


def _fingerprint_regular_nofollow(path: Path) -> Fingerprint:
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Canonical source changed while it was opened",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise StoryArcPlacementSafetyError(
                "source_changed",
                "Canonical source changed while it was opened",
            )
        fingerprint = _fingerprint_regular_descriptor(descriptor)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Canonical source changed while it was fingerprinted",
        ) from exc
    if after.st_dev != before.st_dev or after.st_ino != before.st_ino:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Canonical source changed while it was fingerprinted",
        )
    return fingerprint


def _fingerprint_regular_at(parent_fd: int, name: str) -> Fingerprint:
    before = _entry_lstat_at(parent_fd, name)
    if not stat.S_ISREG(before.st_mode):
        raise StoryArcPlacementSafetyError(
            "not_regular_file",
            "Story-arc artifact is not a regular file",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Story-arc artifact changed while it was opened",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Story-arc artifact changed while it was opened",
            )
        fingerprint = _fingerprint_regular_descriptor(descriptor)
    finally:
        os.close(descriptor)
    after = _entry_lstat_at(parent_fd, name)
    if after.st_dev != before.st_dev or after.st_ino != before.st_ino:
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Story-arc artifact changed while it was fingerprinted",
        )
    return fingerprint


def _fingerprint_target_at(
    parent: _SecureParentDirectory,
    target_name: str,
    *,
    canonical_path: Path | None,
) -> Fingerprint:
    before = _entry_lstat_at(parent.parent_fd, target_name)
    if not stat.S_ISLNK(before.st_mode):
        return _fingerprint_regular_at(parent.parent_fd, target_name)
    if canonical_path is None:
        raise StoryArcPlacementSafetyError(
            "canonical_source_required",
            "Managed story-arc symlink removal requires its canonical source",
        )
    link_target = os.readlink(target_name, dir_fd=parent.parent_fd)
    after = _entry_lstat_at(parent.parent_fd, target_name)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mtime_ns != after.st_mtime_ns
        or os.readlink(target_name, dir_fd=parent.parent_fd) != link_target
    ):
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Story-arc symlink changed while it was fingerprinted",
        )
    prospective = Path(link_target)
    if not prospective.is_absolute():
        prospective = parent.path / prospective
    try:
        resolved = prospective.resolve(strict=True)
        canonical = canonical_path.resolve(strict=True)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "dangling_symlink",
            "Story-arc symlink target is unavailable",
        ) from exc
    if resolved != canonical:
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed story-arc symlink no longer resolves to its canonical source",
        )
    return {
        "schema_version": 1,
        "kind": "symlink",
        "link_target": link_target,
        "device": after.st_dev,
        "inode": after.st_ino,
        "mtime_ns": after.st_mtime_ns,
        "content": _fingerprint_regular_nofollow(canonical),
    }


def _validate_existing_representation_at(
    mode: StoryArcPlacementMode,
    symlink_style: StoryArcSymlinkStyle | None,
    source_path: Path,
    source_fingerprint: Fingerprint,
    target_name: str,
    parent: _SecureParentDirectory,
) -> None:
    target_stat = _entry_lstat_at(parent.parent_fd, target_name)
    if mode is StoryArcPlacementMode.SYMLINK:
        if not stat.S_ISLNK(target_stat.st_mode):
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink was replaced",
            )
        link_target = os.readlink(target_name, dir_fd=parent.parent_fd)
        expected = (
            str(source_path)
            if symlink_style is StoryArcSymlinkStyle.ABSOLUTE
            else os.path.relpath(source_path, start=parent.path.resolve(strict=True))
        )
        if link_target != expected:
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink style or target changed",
            )
        return
    if not stat.S_ISREG(target_stat.st_mode):
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed story-arc artifact type changed",
        )
    if mode is StoryArcPlacementMode.HARDLINK and (
        target_stat.st_dev != source_fingerprint.get("device")
        or target_stat.st_ino != source_fingerprint.get("inode")
    ):
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed hardlink no longer references its canonical source",
        )


def _validate_removal_representation_at(
    mode: StoryArcPlacementMode,
    parent: _SecureParentDirectory,
    target_name: str,
) -> None:
    target_stat = _entry_lstat_at(parent.parent_fd, target_name)
    if mode is StoryArcPlacementMode.SYMLINK:
        if not stat.S_ISLNK(target_stat.st_mode):
            raise StoryArcPlacementSafetyError(
                "fingerprint_mismatch",
                "Managed story-arc symlink was replaced",
            )
        return
    if not stat.S_ISREG(target_stat.st_mode):
        raise StoryArcPlacementSafetyError(
            "fingerprint_mismatch",
            "Managed story-arc artifact type changed after it was recorded",
        )


def _case_only_collision_at(parent_fd: int, target_name: str) -> str | None:
    target_key = target_name.casefold()
    try:
        with os.scandir(parent_fd) as entries:
            for index, child in enumerate(entries, start=1):
                if index > _MAX_CASE_SCAN_ENTRIES:
                    raise StoryArcPlacementSafetyError(
                        "directory_scan_limit",
                        "Story-arc collision scan exceeded its bounded entry limit",
                    )
                if child.name != target_name and child.name.casefold() == target_key:
                    return child.name
    except StoryArcPlacementError:
        raise
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "collision_scan_failed",
            "Story-arc destination collision scan failed",
        ) from exc
    return None


def _fingerprint_regular(path: Path) -> Fingerprint:
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise StoryArcPlacementSafetyError(
            "not_regular_file",
            "Story-arc artifact is not a regular file",
        )
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    after = path.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise StoryArcPlacementSafetyError(
            "source_changed",
            "Story-arc artifact changed while it was fingerprinted",
        )
    return {
        "schema_version": 1,
        "kind": "regular",
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "device": after.st_dev,
        "inode": after.st_ino,
        "sha256": digest.hexdigest(),
    }


def _fingerprint_target(path: Path) -> Fingerprint:
    if not path.is_symlink():
        return _fingerprint_regular(path)
    link_stat = path.lstat()
    link_target = os.readlink(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "dangling_symlink",
            "Story-arc symlink target is unavailable",
        ) from exc
    return {
        "schema_version": 1,
        "kind": "symlink",
        "link_target": link_target,
        "device": link_stat.st_dev,
        "inode": link_stat.st_ino,
        "mtime_ns": link_stat.st_mtime_ns,
        "content": _fingerprint_regular(resolved),
    }


def _target_content_matches_source(
    target_path: Path,
    source_path: Path,
    source_fingerprint: Fingerprint,
) -> bool:
    try:
        if target_path.is_symlink() and target_path.resolve(strict=True) == source_path:
            return True
        if target_path.is_symlink() or not target_path.is_file():
            return False
        if target_path.stat().st_size != source_fingerprint.get("size"):
            return False
        return _fingerprint_regular(target_path).get("sha256") == source_fingerprint.get("sha256")
    except (OSError, StoryArcPlacementError):
        return False


def _case_only_collision(target_path: Path) -> Path | None:
    parent = target_path.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        return None
    key = target_path.name.casefold()
    try:
        for index, child in enumerate(parent.iterdir(), start=1):
            if index > _MAX_CASE_SCAN_ENTRIES:
                raise StoryArcPlacementSafetyError(
                    "directory_scan_limit",
                    "Story-arc collision scan exceeded its bounded entry limit",
                )
            if child.name != target_path.name and child.name.casefold() == key:
                return child
    except StoryArcPlacementError:
        raise
    except OSError as exc:
        raise StoryArcPlacementSafetyError(
            "collision_scan_failed",
            "Story-arc destination collision scan failed",
        ) from exc
    return None


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _raise_from_preview(preview: StoryArcPlacementPreview) -> None:
    reason = preview.reason or "Story-arc placement preview blocked execution"
    code = preview.collision.value
    if preview.collision in {
        StoryArcCollisionKind.DIFFERENT_CONTENT,
        StoryArcCollisionKind.CASE_ONLY,
    }:
        raise StoryArcPlacementCollisionError(code, reason)
    raise StoryArcPlacementSafetyError(code, reason)


def _raise_if_cancelled(callback: CancellationRequested | None) -> None:
    if callback is not None and callback():
        raise StoryArcPlacementCancellationError


def _record_journal(
    journal: StoryArcPlacementJournal | None,
    event: StoryArcPlacementJournalEvent,
) -> None:
    if journal is not None:
        journal(event)


def _record_failure(
    journal: StoryArcPlacementJournal | None,
    prepared: StoryArcPlacementJournalEvent,
    failure_code: str,
) -> None:
    if journal is None:
        return
    with suppress(Exception):
        journal(
            StoryArcPlacementJournalEvent(
                stage="failed",
                operation=prepared.operation,
                issue_story_arc_id=prepared.issue_story_arc_id,
                mode=prepared.mode,
                target_path=prepared.target_path,
                source_fingerprint=dict(prepared.source_fingerprint),
                target_fingerprint={},
                failure_code=failure_code,
            )
        )
        # Preserve the filesystem failure if this secondary notification fails;
        # the durable prepared action still supports reconciliation.


def _categorized_os_error(
    mode: StoryArcPlacementMode,
    error: OSError,
) -> StoryArcPlacementError:
    if error.errno == errno.EXDEV and mode is StoryArcPlacementMode.HARDLINK:
        return StoryArcPlacementSafetyError(
            "cross_device",
            "Hardlink source and story-arc destination are on different filesystems",
        )
    if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
        return StoryArcPlacementCollisionError(
            "destination_exists",
            "Story-arc destination exists and was not overwritten",
        )
    if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return StoryArcPlacementSafetyError(
            "permission_denied",
            "Story-arc destination is not writable",
        )
    label = "Hardlink" if mode is StoryArcPlacementMode.HARDLINK else "Story-arc placement"
    return StoryArcPlacementSafetyError(
        "filesystem_error",
        f"{label} filesystem operation failed",
    )
