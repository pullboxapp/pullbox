"""User-requested recovery of current automatic Story Arc file work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, true, update

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementSyncService,
    validate_story_arc_placement_policy_input,
)
from pullbox.services.story_arc_sync_queue import (
    _automatic_sync_enabled,
    _desired_generation,
    current_story_arc_sync_conditions,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

RETRY_BATCH_SIZE = 200


def current_story_arc_sync_work_ids(
    story_arc_id: int | None = None, *, include_imports: bool = False
) -> Select[int]:
    # Old revisions and source generations are history, not new user decisions.
    return (
        select(func.max(StoryArcSyncWork.id))
        .join(IssueStoryArc, IssueStoryArc.id == StoryArcSyncWork.issue_story_arc_id)
        .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
        .join(LibraryFile, LibraryFile.id == StoryArcSyncWork.library_file_id)
        .where(
            StoryArc.id == story_arc_id if story_arc_id is not None else true(),
            LibraryFile.issue_id == IssueStoryArc.issue_id,
            true() if include_imports else IssueStoryArc.sync_eligible.is_(True),
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
            true() if include_imports else StoryArcSyncWork.origin_import_job_id.is_(None),
            true() if include_imports else StoryArcSyncWork.claimable.is_(True),
            current_story_arc_sync_conditions(),
        )
        .group_by(StoryArcSyncWork.issue_story_arc_id, StoryArcSyncWork.library_file_id)
    )


@dataclass(frozen=True)
class StoryArcFileSyncStatus:
    arc_id: int
    revision: int
    enabled: bool
    destination: str
    total: int
    completed: int
    pending: int
    failed: int
    destination_failures: int

    @property
    def percent(self) -> int:
        return int(100 * self.completed / self.total) if self.total else 0


async def load_story_arc_file_sync_status(
    session: AsyncSession, story_arc_id: int
) -> StoryArcFileSyncStatus:
    """Project current file work without inspecting archives or loading history."""
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise NotFoundError("StoryArc", story_arc_id)
    rows = (
        await session.execute(
            select(StoryArcSyncWork.state, StoryArcSyncWork.last_error_code, func.count())
            .where(StoryArcSyncWork.id.in_(current_story_arc_sync_work_ids(story_arc_id)))
            .group_by(StoryArcSyncWork.state, StoryArcSyncWork.last_error_code)
        )
    ).all()
    counts: dict[StoryArcSyncWorkState, int] = {}
    destination_failures = 0
    for state, code, count in rows:
        counts[state] = counts.get(state, 0) + count
        if state == StoryArcSyncWorkState.FAILED and code in {
            "root_unavailable",
            "destination_root_unavailable",
            "target_library_root_unavailable",
        }:
            destination_failures += count
    return StoryArcFileSyncStatus(
        arc_id=arc.id,
        revision=arc.revision,
        enabled=_automatic_sync_enabled(arc),
        destination=str((arc.policy_snapshot or {}).get("destination_root") or ""),
        total=sum(counts.values()),
        completed=counts.get(StoryArcSyncWorkState.COMPLETED, 0),
        pending=sum(
            counts.get(state, 0)
            for state in (
                StoryArcSyncWorkState.QUEUED,
                StoryArcSyncWorkState.RUNNING,
                StoryArcSyncWorkState.RETRY_WAIT,
            )
        ),
        failed=counts.get(StoryArcSyncWorkState.FAILED, 0),
        destination_failures=destination_failures,
    )


async def retry_story_arc_files(
    session: AsyncSession, story_arc_id: int, *, expected_revision: int
) -> int:
    """Recheck the destination and reopen only current failed automatic work."""
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise NotFoundError("StoryArc", story_arc_id)
    if arc.revision != expected_revision:
        raise ValidationError("The Story Arc changed. Refresh the page before retrying.")
    if not _automatic_sync_enabled(arc):
        raise ValidationError("Automatic file synchronization is not enabled for this Story Arc.")
    policy = await StoryArcPlacementSyncService().get_policy(session, story_arc_id)
    await validate_story_arc_placement_policy_input(
        session,
        StoryArcPlacementPolicyInput(
            mode=policy.mode,
            target_library_root_id=policy.target_library_root_id,
            destination_root=policy.destination_root,
            folder_template=policy.folder_template,
            file_template=policy.file_template,
            symlink_style=policy.symlink_style,
            synchronize=policy.synchronize,
        ),
        revision=expected_revision,
    )
    rows = (
        await session.execute(
            select(StoryArcSyncWork, LibraryFile, IssueStoryArc)
            .join(LibraryFile, LibraryFile.id == StoryArcSyncWork.library_file_id)
            .join(IssueStoryArc, IssueStoryArc.id == StoryArcSyncWork.issue_story_arc_id)
            .where(
                StoryArcSyncWork.id.in_(current_story_arc_sync_work_ids(story_arc_id)),
                StoryArcSyncWork.state == StoryArcSyncWorkState.FAILED,
            )
            .order_by(StoryArcSyncWork.id)
            .limit(RETRY_BATCH_SIZE)
        )
    ).all()
    ids = [
        work.id
        for work, source, member in rows
        if _desired_generation(source, member, arc)[0] == work.desired_generation
    ]
    if not ids:
        return 0
    retried = (
        await session.scalars(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id.in_(ids),
                StoryArcSyncWork.id.in_(current_story_arc_sync_work_ids(story_arc_id)),
                StoryArcSyncWork.state == StoryArcSyncWorkState.FAILED,
            )
            .values(
                state=StoryArcSyncWorkState.QUEUED,
                attempt_count=0,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error_code=None,
                last_error_category=None,
                last_error_detail=None,
                updated_at=datetime.now(UTC),
            )
            .returning(StoryArcSyncWork.id)
        )
    ).all()
    return len(retried)
