"""Preview and confirm deletion of a reading list, never library or placement files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, false, func, or_, select, text, update

from pullbox.core.config_resolver import get_application_secret
from pullbox.models.audit_log import AuditEventType, AuditLog
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementOwnership,
    StoryArcResolutionState,
)
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_member_review import resolved_member_issue_ids
from pullbox.services.story_arc_service import StoryArcConflictError, StoryArcNotFoundError
from pullbox.utilities.models import JobState, UtilityJob

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_RECEIPT_BLOCKER = (
    "An arc file removal has an unfinished Trash receipt. Review arc files and "
    "select the interrupted removal before deleting this arc."
)
_ACTIVE_CLEANUP = "Arc cleanup is queued or running. Check its progress in Utilities."
_MEMBER_RECEIPT = (
    "A library file has interrupted cleanup. Finish its retained journal before deleting this arc."
)


@dataclass(frozen=True)
class ArcDeleteReview:
    name: str
    entries: int
    issues: int
    series: int
    files: int
    shared_issues: int
    placements: int
    managed_placements: int
    referenced_placements: int
    blocker: str | None


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt="story-arc-delete-record-only-v1")


async def _snapshot(
    db: AsyncSession,
    arc_id: int,
    user_id: int,
    *,
    lock: bool,
    omit: frozenset[int] = frozenset(),
    omit_files: frozenset[int] = frozenset(),
    cleanup_job: str | None = None,
) -> tuple[ArcDeleteReview, str]:
    sqlite = db.get_bind().dialect.name == "sqlite"
    if lock and sqlite:
        # Reserve SQLite's writer before checking child rows; PostgreSQL locks
        # each parent before its children so a concurrent insert cannot slip in.
        await db.execute(update(StoryArc).where(false()).values(revision=StoryArc.revision))
    query = select(StoryArc).where(StoryArc.id == arc_id).execution_options(populate_existing=True)
    arc = await db.scalar(query.with_for_update() if lock else query)
    if arc is None:
        raise StoryArcNotFoundError("Story Arc not found.")
    member_ids = select(IssueStoryArc.id).where(IssueStoryArc.story_arc_id == arc_id)
    member_query = (
        select(
            IssueStoryArc.id,
            IssueStoryArc.issue_id,
            IssueStoryArc.resolution_state,
        )
        .where(IssueStoryArc.story_arc_id == arc_id)
        .order_by(IssueStoryArc.id)
    )
    members = (await db.execute(member_query.with_for_update() if lock else member_query)).all()
    placement_query = (
        select(
            StoryArcPlacement.id,
            StoryArcPlacement.placement_path,
            StoryArcPlacement.mode,
            StoryArcPlacement.ownership,
            StoryArcPlacement.creating_action_id,
            StoryArcPlacement.operation_token,
            StoryArcPlacement.source_import_job_id,
            StoryArcPlacement.last_result["trash_receipt"].as_string().label("receipt"),
        )
        .where(StoryArcPlacement.issue_story_arc_id.in_(member_ids))
        .order_by(StoryArcPlacement.id)
    )
    placements = (
        await db.execute(placement_query.with_for_update() if lock else placement_query)
    ).all()
    work_query = (
        select(StoryArcSyncWork.state, StoryArcSyncWork.origin_import_action_id)
        .where(StoryArcSyncWork.issue_story_arc_id.in_(member_ids))
        .order_by(StoryArcSyncWork.id)
    )
    work = (await db.execute(work_query.with_for_update() if lock else work_query)).all()
    import_binding = await db.scalar(
        select(ImportedStoryArc.id)
        .join(ImportJob)
        .where(
            or_(
                ImportedStoryArc.materialized_story_arc_id == arc_id,
                (ImportedStoryArc.proposed_story_arc_id == arc_id)
                & ImportJob.status.not_in(
                    [
                        ImportJobStatus.COMPLETED,
                        ImportJobStatus.ROLLED_BACK,
                        ImportJobStatus.CANCELLED,
                        ImportJobStatus.FAILED,
                    ]
                ),
            )
        )
        .limit(1)
    )
    marker = (arc.diagnostics or {}).get("catalog_initial_placements")
    pending = (arc.diagnostics or {}).get("reviewed_cleanup")
    blocker = None
    if (
        pending
        and pending != cleanup_job
        and await db.scalar(
            select(UtilityJob.id).where(
                UtilityJob.id == pending,
                UtilityJob.state.in_(
                    [
                        JobState.QUEUED,
                        JobState.RUNNING,
                        JobState.CANCELLING,
                        JobState.PAUSING,
                        JobState.PAUSED,
                    ]
                ),
            )
        )
    ):
        blocker = _ACTIVE_CLEANUP
    elif (arc.diagnostics or {}).get("pending_member_cleanup") and (
        not cleanup_job or cleanup_job != pending
    ):
        blocker = _MEMBER_RECEIPT
    elif (
        arc.source_import_job_id is not None
        or import_binding is not None
        or any(p.creating_action_id or p.source_import_job_id for p in placements)
        or any(w.origin_import_action_id for w in work)
    ):
        blocker = (
            "This arc is linked to import or rollback evidence. "
            "Archive it instead to keep that history intact."
        )
    elif (
        any(p.operation_token for p in placements)
        or any(
            w.state
            not in {
                StoryArcSyncWorkState.COMPLETED,
                StoryArcSyncWorkState.CANCELLED,
                StoryArcSyncWorkState.FAILED,
            }
            for w in work
        )
        or (isinstance(marker, dict) and marker.get("state") == "pending")
    ):
        blocker = "Arc file work is unfinished. Finish or recover it, then review deletion again."
    elif any(p.receipt for p in placements if p.id not in omit):
        blocker = _RECEIPT_BLOCKER
    elif sqlite and not await db.scalar(text("PRAGMA foreign_keys")):
        blocker = "SQLite foreign key enforcement is off. Restore it before deleting an arc."
    placements = [p for p in placements if p.id not in omit]
    managed_count = sum(p.ownership == StoryArcPlacementOwnership.MANAGED for p in placements)
    review = ArcDeleteReview(
        arc.name,
        len(members),
        *await _retained_library_counts(db, arc_id, omit_files),
        len(placements),
        managed_count,
        len(placements) - managed_count,
        blocker,
    )
    binding = [
        user_id,
        arc_id,
        arc.revision,
        asdict(review),
        [tuple(m) for m in members],
        [tuple(p) for p in placements],
    ]
    return review, hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


async def _retained_library_counts(
    db: AsyncSession, arc_id: int, omit_files: frozenset[int]
) -> tuple[int, int, int, int]:
    resolved = resolved_member_issue_ids(arc_id)
    files = select(func.count(LibraryFile.id)).where(LibraryFile.issue_id.in_(resolved))
    if omit_files:
        files = files.where(LibraryFile.id.not_in(omit_files))
    shared = select(func.count(func.distinct(IssueStoryArc.issue_id))).where(
        IssueStoryArc.story_arc_id != arc_id,
        IssueStoryArc.issue_id.in_(resolved),
        IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
    )
    counts = await db.execute(
        select(
            func.count(Issue.id),
            func.count(func.distinct(Issue.series_id)),
            files.scalar_subquery(),
            shared.scalar_subquery(),
        ).where(Issue.id.in_(resolved))
    )
    issues, series, canonical, shared_issues = counts.one()
    return issues, series, canonical, shared_issues


async def preview_arc_deletion(db: AsyncSession, arc_id: int, user_id: int) -> dict[str, object]:
    review, binding = await _snapshot(db, arc_id, user_id, lock=False)
    arc = await db.get(StoryArc, arc_id)
    return {
        **asdict(review),
        "pending_member_cleanup": (arc.diagnostics or {}).get("pending_member_cleanup")
        if arc and review.blocker == _MEMBER_RECEIPT
        else None,
        "active_cleanup": review.blocker == _ACTIVE_CLEANUP,
        "cleanup_allowed": review.blocker in (None, _RECEIPT_BLOCKER),
        "token": None if review.blocker else _signer().dumps(binding),
    }


async def review_arc_placement_paths(
    db: AsyncSession, arc_id: int, after: int
) -> dict[str, object]:
    """Page saved directory entries without opening, resolving or hashing a comic."""
    if await db.scalar(select(StoryArc.id).where(StoryArc.id == arc_id)) is None:
        raise StoryArcNotFoundError("Story Arc not found.")
    page_size = 20
    rows = (
        await db.execute(
            select(
                StoryArcPlacement.id,
                StoryArcPlacement.placement_path,
                StoryArcPlacement.mode,
                StoryArcPlacement.ownership,
                LibraryFile.id.is_not(None).label("registered"),
                StoryArcPlacement.source_fingerprint,
                StoryArcPlacement.last_result,
                StoryArcPlacement.source_import_job_id,
                StoryArcPlacement.creating_action_id,
            )
            .join(IssueStoryArc, IssueStoryArc.id == StoryArcPlacement.issue_story_arc_id)
            .outerjoin(LibraryFile, LibraryFile.file_path == StoryArcPlacement.placement_path)
            .where(IssueStoryArc.story_arc_id == arc_id, StoryArcPlacement.id > after)
            .order_by(StoryArcPlacement.id)
            .limit(page_size + 1)
        )
    ).all()
    items = [
        {
            "id": row.id,
            "path": row.placement_path,
            "mode": row.mode.value,
            "ownership": row.ownership.value,
            "registered": row.registered,
            "selectable": bool(
                row.ownership == StoryArcPlacementOwnership.MANAGED
                and not row.registered
                and not row.source_import_job_id
                and not row.creating_action_id
                and row.source_fingerprint
                and (row.last_result or {}).get("target_fingerprint")
            ),
            "interrupted": bool((row.last_result or {}).get("trash_receipt")),
        }
        for row in rows[:page_size]
    ]
    return {"items": items, "next_after": rows[page_size - 1].id if len(rows) > page_size else None}


async def delete_reviewed_arc(db: AsyncSession, arc_id: int, user_id: int, token: str) -> None:
    review, binding = await _snapshot(db, arc_id, user_id, lock=True)
    if review.blocker:
        raise StoryArcConflictError(review.blocker)
    try:
        expected = _signer().loads(token, max_age=900)
    except BadSignature as exc:
        raise StoryArcConflictError(
            "This preview is invalid or expired. Review deletion again."
        ) from exc
    if expected != binding:
        raise StoryArcConflictError("This arc changed after the preview. Review deletion again.")
    await _delete_arc_record(db, arc_id, user_id, review)


async def _delete_arc_record(
    db: AsyncSession,
    arc_id: int,
    user_id: int,
    review: ArcDeleteReview,
    *,
    job_id: str | None = None,
    series_ids: list[int] | None = None,
    removed_series_files: int = 0,
) -> None:
    # Delete only the parent. FK cascades release the arc's own rows, never
    # canonical issues or files. The audit commits in the same transaction.
    await db.execute(delete(StoryArc).where(StoryArc.id == arc_id))
    db.add(
        AuditLog(
            event_type=AuditEventType.STORY_ARC_RECORD_DELETED,
            user_id=user_id,
            detail=f"Deleted arc, series records and {removed_series_files} reviewed files."
            if removed_series_files
            else "Deleted the Story Arc and explicitly selected series records; kept all files."
            if series_ids
            else "Deleted the Story Arc record after reviewed arc file cleanup."
            if job_id
            else "Deleted the Story Arc record only; no files removed.",
            metadata_json=json.dumps(
                {
                    "story_arc_id": arc_id,
                    **asdict(review),
                    **({"job_id": job_id} if job_id else {}),
                    **({"deleted_series_ids": series_ids} if series_ids else {}),
                    **(
                        {"removed_series_files": removed_series_files}
                        if removed_series_files
                        else {}
                    ),
                }
            ),
        )
    )
    await db.flush()
