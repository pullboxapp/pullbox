"""Canonical file detachment for the reviewed Story Arc removal worker."""

import asyncio
import os
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.import_job import ImportedFile, ImportJobAction, ImportJobActionStatus
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.library_removal import LibraryRemoval
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcResolutionState,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork
from pullbox.services.archive_metadata_publication import _fingerprint
from pullbox.services.library_conversion_files import matches
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.services.library_removal import RemovalPlan, stage_removal
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation


class _Copy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: int
    path: Path
    root_id: int
    root_path: Path
    fingerprint: tuple[int, int, int, int, int, int]


class MemberRemovalReview(BaseModel):
    """Item-level evidence. The owning job must explicitly authorize the disposition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    arc_id: int
    revision: int
    member_id: int
    issue_id: int
    series_id: int
    parent_scope_id: int | None = None
    file: _Copy
    remaining: tuple[_Copy, ...]
    shared_arcs: tuple[int, ...]


def _inspect(book: LibraryFile, root: LibraryRoot, *, removing: bool) -> _Copy:
    path, root_path = Path(book.file_path), Path(root.path)
    try:
        if (
            path == root_path
            or any(ord(c) < 32 or ord(c) == 127 for c in book.file_path)
            or not path.is_absolute()
            or path.resolve(strict=True) != path
            or not root_path.is_absolute()
            or (root_path.resolve(strict=True) != root_path or not path.is_relative_to(root_path))
        ):
            raise ValueError
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or not os.access(path, os.R_OK)
            or info.st_size != book.file_size
            or (datetime.fromtimestamp(info.st_mtime, UTC) != book.file_modified_at)
        ):
            raise ValueError
        if removing and (info.st_nlink != 1 or not os.access(path.parent, os.W_OK | os.X_OK)):
            raise ValueError
    except (OSError, RuntimeError, ValueError, OverflowError) as exc:
        raise StoryArcConflictError(
            "A library file is missing, changed, linked or inaccessible. Review the files again."
        ) from exc
    return _Copy(
        id=book.id,
        path=path,
        root_id=root.id,
        root_path=root_path,
        fingerprint=(
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
        ),
    )


def _check_nested_roots(copy: _Copy, other_roots: Sequence[str]) -> None:
    # Legacy configurations may contain nested roots. The outer registration
    # cannot authorize removal inside a different, more specific root.
    try:
        for other in other_roots:
            root = Path(other).resolve(strict=False)
            if root.is_relative_to(copy.root_path) and copy.path.is_relative_to(root):
                raise StoryArcConflictError(
                    "Another library root covers this file. Review its location before removal."
                )
    except (OSError, RuntimeError, ValueError) as exc:
        raise StoryArcConflictError("Library root locations could not be verified.") from exc


async def _guard_work(db: AsyncSession, issue_id: int, book: LibraryFile) -> None:
    await ensure_no_active_import_file_mutation(db)
    target = book.file_path
    retained = (
        select(ImportedFile.id)
        .where(or_(ImportedFile.library_file_id == book.id, ImportedFile.file_path == target))
        .exists(),
        select(ImportJobAction.id)
        .where(
            ImportJobAction.action_type.in_(
                ("library_file_registered", "library_file_placement_started")
            ),
            ImportJobAction.status != ImportJobActionStatus.ROLLED_BACK,
            or_(
                ImportJobAction.payload["library_file_id"].as_integer() == book.id,
                ImportJobAction.payload["destination_path"].as_string() == target,
                ImportJobAction.payload["original_source_path"].as_string() == target,
            ),
        )
        .exists(),
        select(StoryArcPlacement.id)
        .where(
            or_(
                StoryArcPlacement.library_file_id == book.id,
                StoryArcPlacement.placement_path == target,
            )
        )
        .exists(),
        select(StoryArcSyncWork.id).where(StoryArcSyncWork.library_file_id == book.id).exists(),
    )
    if await db.scalar(select(or_(*retained))):
        raise StoryArcConflictError(
            "This file is linked to import history or arc file work. Keep its original in place."
        )
    active = (
        select(DownloadHistory.id)
        .where(
            DownloadHistory.issue_id == issue_id,
            or_(
                DownloadHistory.state.not_in(
                    (DownloadState.FAILED, DownloadState.COMPLETED, DownloadState.IMPORTED)
                ),
                (DownloadHistory.state == DownloadState.COMPLETED)
                & DownloadHistory.imported_at.is_(None),
            ),
        )
        .exists(),
        select(DirectAcquisitionAttempt.id)
        .where(
            DirectAcquisitionAttempt.issue_id == issue_id,
            DirectAcquisitionAttempt.state.not_in(
                (
                    DirectAcquisitionState.COMPLETED,
                    DirectAcquisitionState.FAILED,
                    DirectAcquisitionState.CANCELLED,
                )
            ),
        )
        .exists(),
        select(PendingMatch.id)
        .where(PendingMatch.issue_id == issue_id, PendingMatch.status == PendingMatchStatus.PENDING)
        .exists(),
    )
    if await db.scalar(select(or_(*active))):
        raise StoryArcConflictError(
            "This issue still has acquisition work. Resolve it before removing its file."
        )


async def review_member_removal(
    db: AsyncSession,
    arc_id: int,
    file_id: int,
    *,
    shared_arcs: Sequence[int],
    lock: bool = False,
    parent_scope_id: int | None = None,
) -> MemberRemovalReview:
    """Resolve exact membership and verify every surviving copy before predicting ownership."""
    query = (
        select(StoryArc.revision, IssueStoryArc.id, Issue.id, Issue.series_id)
        .select_from(IssueStoryArc)
        .join(StoryArc)
        .join(Issue)
        .join(LibraryFile, LibraryFile.issue_id == Issue.id)
        .where(
            IssueStoryArc.story_arc_id == arc_id,
            LibraryFile.id == file_id,
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
    )
    if parent_scope_id is not None:
        # An outside issue needs two exact relationships: the file belongs to
        # the selected series, and a resolved chapter proves that series is related.
        witness = (
            select(IssueStoryArc.id)
            .join(Issue, Issue.id == IssueStoryArc.issue_id)
            .where(
                IssueStoryArc.story_arc_id == arc_id,
                Issue.series_id == parent_scope_id,
                IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
            )
            .order_by(IssueStoryArc.id)
            .limit(1)
            .correlate(None)
        )
        query = (
            select(StoryArc.revision, IssueStoryArc.id, Issue.id, Issue.series_id)
            .select_from(LibraryFile)
            .join(Issue, Issue.id == LibraryFile.issue_id)
            .join(IssueStoryArc, IssueStoryArc.id == witness.scalar_subquery())
            .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
            .where(LibraryFile.id == file_id, Issue.series_id == parent_scope_id)
        )
    row = (await db.execute(query.with_for_update() if lock else query)).one_or_none()
    if row is None:
        raise StoryArcConflictError("Only an exactly matched member file can be removed.")
    revision, member_id, issue_id, series_id = row
    others = (
        select(IssueStoryArc.story_arc_id, IssueStoryArc.resolution_state)
        .where(IssueStoryArc.issue_id == issue_id, IssueStoryArc.story_arc_id != arc_id)
        .order_by(IssueStoryArc.story_arc_id)
    )
    peers = tuple(
        peer
        for peer, state in (await db.execute(others.with_for_update() if lock else others))
        if state == StoryArcResolutionState.RESOLVED
    )
    if peers != tuple(sorted(shared_arcs)):
        raise StoryArcConflictError("This comic is shared. Review every other arc that uses it.")
    query_files = (
        select(LibraryFile, LibraryRoot)
        .join(LibraryRoot)
        .where(LibraryFile.issue_id == issue_id)
        .order_by(LibraryFile.id)
        .execution_options(populate_existing=True)
    )
    rows = (await db.execute(query_files.with_for_update() if lock else query_files)).all()
    selected = None
    survivors = []
    for book, root in rows:
        removing = book.id == file_id
        if not root.enabled or (
            removing
            and (
                not root.allow_managed_writes or book.storage_mode != LibraryFileStorageMode.MANAGED
            )
        ):
            raise StoryArcConflictError(
                "Referenced files and disabled or read-only roots are protected."
            )
        if removing:
            await _guard_work(db, issue_id, book)
        copy = await asyncio.to_thread(_inspect, book, root, removing=removing)
        if removing:
            other_roots = select(LibraryRoot.path).where(LibraryRoot.id != root.id)
            paths = (await db.scalars(other_roots)).all()
            await asyncio.to_thread(_check_nested_roots, copy, paths)
            selected = copy
        else:
            survivors.append(copy)
    if selected is None:
        raise StoryArcConflictError("The reviewed library file changed.")
    return MemberRemovalReview(
        arc_id=arc_id,
        revision=revision,
        member_id=member_id,
        issue_id=issue_id,
        series_id=series_id,
        parent_scope_id=parent_scope_id,
        file=selected,
        remaining=tuple(survivors),
        shared_arcs=peers,
    )


async def stage_member_removal(
    db: AsyncSession, review: MemberRemovalReview, plan: RemovalPlan
) -> None:
    """Stage one previously reserved file and update issue intent in the same transaction.

    The caller commits before complete_removal; loss of this transaction leaves
    an intended journal whose recovery restores the original path.
    """
    await lock_file_mutation_admission(db)
    current = await review_member_removal(
        db,
        review.arc_id,
        review.file.id,
        shared_arcs=review.shared_arcs,
        lock=True,
        parent_scope_id=review.parent_scope_id,
    )
    journal = await db.scalar(
        select(LibraryRemoval.plan_json)
        .where(
            LibraryRemoval.operation_id == str(plan.operation_id),
            LibraryRemoval.active.is_(True),
            LibraryRemoval.state == "intended",
        )
        .with_for_update()
    )
    if (
        current != review
        or plan.source != review.file.path
        or plan.fingerprint != review.file.fingerprint
        or plan.root_id != review.file.root_id
        or plan.root_path != review.file.root_path
        or plan.disposition not in {"trash", "delete"}
        or journal != plan.model_dump_json()
    ):
        raise StoryArcConflictError(
            "The reviewed member, file or removal plan changed. Nothing was removed."
        )
    issue = await db.get(Issue, review.issue_id, populate_existing=True)
    book = await db.get(LibraryFile, review.file.id)
    if issue is None or book is None:
        raise StoryArcConflictError("The reviewed issue or file no longer exists.")
    # The job can catch an item error and commit its report. Keep the journal
    # recoverable unless BOTH the staged file and the DB detachment succeeded.
    async with db.begin_nested():
        await stage_removal(db, plan.operation_id)
        if not matches(await asyncio.to_thread(_fingerprint, plan.stage), review.file.fingerprint):
            raise StoryArcConflictError(
                "The staged file changed. Its registration was kept for recovery."
            )
        await db.delete(book)
        issue.status = IssueStatus.OWNED if review.remaining else IssueStatus.SKIPPED
        if not review.remaining:
            issue.manual_skip = True
        await db.flush()
