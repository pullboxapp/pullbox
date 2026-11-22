"""Run an arc job's signed original-file selection through its removal journal."""

import asyncio
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.library_removal import LibraryRemoval
from pullbox.models.story_arc import StoryArc
from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.services.library_removal import (
    decode_removal,
    prepare_removal,
    record_removal,
    recover_uncommitted_removal,
)
from pullbox.services.library_removal_cleanup import complete_removal
from pullbox.services.library_removal_workspaces import prune_workspaces
from pullbox.services.story_arc_deletion import _snapshot
from pullbox.services.story_arc_member_removal import (
    MemberRemovalReview,
    review_member_removal,
    stage_member_removal,
)
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem


async def _owner(
    db: AsyncSession, config: dict[str, Any], item_id: str
) -> tuple[StoryArc, UtilityJobItem]:
    await lock_file_mutation_admission(db)
    item = await db.get(UtilityJobItem, item_id, populate_existing=True)
    if item is None:
        raise StoryArcConflictError("Cleanup item not found.")
    job = await db.get(UtilityJob, item.job_id, populate_existing=True, with_for_update=True)
    arc = await db.get(StoryArc, config["arc_id"], populate_existing=True, with_for_update=True)
    if (
        not arc
        or not job
        or job.state != JobState.RUNNING
        or (arc.diagnostics or {}).get("reviewed_cleanup") != item.job_id
    ):
        raise StoryArcConflictError("Cleanup stopped before starting another file.")
    return arc, item


async def _clear_marker(db: AsyncSession, arc_id: int, operation: str) -> None:
    await lock_file_mutation_admission(db)
    arc = await db.get(StoryArc, arc_id, with_for_update=True, populate_existing=True)
    if (
        arc
        and (arc.diagnostics or {}).get("pending_member_cleanup", {}).get("operation") == operation
    ):
        saved = dict(arc.diagnostics or {})
        del saved["pending_member_cleanup"]
        arc.diagnostics = saved
    await db.commit()


async def finish_member_receipt(db: AsyncSession, arc_id: int, operation: str) -> str:
    """Only finish a previously detached payload, or restore an uncommitted intent."""
    state = await recover_uncommitted_removal(db, UUID(operation))
    if state == "detached":
        state = await complete_removal(db, UUID(operation))
    if state not in {"complete", "abandoned"}:
        raise StoryArcConflictError(
            "The retained file no longer matches its journal. "
            "Inspect the original and Trash before retrying."
        )
    await _clear_marker(db, arc_id, operation)
    return state


async def enqueue_member_recovery(
    db: AsyncSession, manager: JobQueueManager, arc_id: int, operation: UUID, username: str
) -> UtilityJob:
    await lock_file_mutation_admission(db)
    arc = await db.get(StoryArc, arc_id, with_for_update=True, populate_existing=True)
    diagnostics = (arc.diagnostics or {}) if arc else {}
    marker = diagnostics.get("pending_member_cleanup") or {}
    if not arc or marker.get("operation") != str(operation):
        raise StoryArcConflictError("The retained cleanup changed. Refresh the preview.")
    previous = await db.get(UtilityJob, diagnostics["reviewed_cleanup"])
    if previous and previous.state not in {JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED}:
        raise StoryArcConflictError(
            "This cleanup job is still active. Open Utilities to check or resume it."
        )
    job = await manager.create_job(
        db,
        JobType.STORY_ARC_CLEANUP,
        "Finish file cleanup: " + arc.name,
        {
            "arc_id": arc_id,
            "receipt_only": str(operation),
            "targets": [{"file_path": marker["path"]}],
        },
        username,
    )
    arc.diagnostics = {**diagnostics, "reviewed_cleanup": job.id}
    await db.flush()
    return job


async def recover_member_item(
    db: AsyncSession, config: dict[str, Any], item_id: str
) -> dict[str, Any]:
    arc, _ = await _owner(db, config, item_id)
    operation = config["receipt_only"]
    marker = (arc.diagnostics or {}).get("pending_member_cleanup")
    if marker and marker.get("operation") != operation:
        raise StoryArcConflictError("The retained cleanup changed.")
    if not marker:
        state = await db.scalar(
            select(LibraryRemoval.state).where(LibraryRemoval.operation_id == operation)
        )
        if state not in {"complete", "abandoned"}:
            raise StoryArcConflictError("The arc no longer identifies this pending file cleanup.")
    arc_id = arc.id
    await db.commit()
    state = await finish_member_receipt(db, arc_id, operation)
    return {"journal_state": state, "arc_preserved": True}


async def run_member_item(
    db: AsyncSession, config: dict[str, Any], index: int, item_id: str
) -> dict[str, Any]:
    arc, item = await _owner(db, config, item_id)
    targets = config["targets"]
    review = MemberRemovalReview.model_validate(targets[index]["original"])
    if (
        item.item_index != index
        or item.operation != "remove_member_file"
        or item.file_path != str(review.file.path)
    ):
        raise StoryArcConflictError("The queued file no longer matches the reviewed selection.")
    before = json.loads(item.before_state or "{}")
    operation = before.get("removal_operation")
    prior = targets[:index]
    if prior:
        done = set(
            await db.scalars(
                select(UtilityJobItem.item_index).where(
                    UtilityJobItem.job_id == item.job_id,
                    UtilityJobItem.state == ItemState.COMPLETED,
                    UtilityJobItem.item_index < index,
                )
            )
        )
        if done != set(range(index)):
            raise StoryArcConflictError(
                "An earlier cleanup item was not completed; this file was kept."
            )
        removed = {t["id"] for t in prior if "original" in t}
        review = review.model_copy(
            update={"remaining": tuple(c for c in review.remaining if c.id not in removed)}
        )
    pending = (arc.diagnostics or {}).get("pending_member_cleanup")
    if pending and pending.get("operation") != operation:
        raise StoryArcConflictError("Finish the interrupted file cleanup first.")
    if operation:
        row = await db.scalar(
            select(LibraryRemoval).where(LibraryRemoval.operation_id == operation)
        )
        if not row or (not pending and row.state != "complete"):
            raise StoryArcConflictError(
                "The previous cleanup was interrupted. Review remaining files again."
            )
        plan = decode_removal(row.plan_json, operation_id=operation)
        if (
            plan.source != review.file.path
            or plan.fingerprint != review.file.fingerprint
            or plan.root_id != review.file.root_id
        ):
            raise StoryArcConflictError(
                "The retained journal does not identify the reviewed comic."
            )
        state = row.state
        await db.commit()
        if state != "intended":
            if await finish_member_receipt(db, review.arc_id, operation) != "complete":
                raise StoryArcConflictError("The original file was restored; review removal again.")
            return {
                "removed": str(plan.source),
                "trash_path": str(plan.trash_path) if plan.trash_path else None,
            }
    else:
        # Don't reserve a changed source or leave a workspace for an ineligible target.
        if (
            await review_member_removal(
                db, review.arc_id, review.file.id, shared_arcs=review.shared_arcs, lock=True
            )
            != review
        ):
            raise StoryArcConflictError(
                "A reviewed library file changed. Review the remaining files."
            )
        trash = await resolve_configured_utility_trash_dir(db)
        if (str(trash) if trash else None) != config["trash"]:
            raise StoryArcConflictError("Trash settings changed. Review file cleanup again.")
        dest = trash / f"arc-cleanup-{item_id}" / review.file.path.name if trash else None
        plan = await asyncio.to_thread(
            prepare_removal,
            review.file.path,
            root_id=review.file.root_id,
            root_path=review.file.root_path,
            disposition="trash" if trash else "delete",
            trash_path=dest,
        )
        try:
            if plan.fingerprint != review.file.fingerprint:
                raise StoryArcConflictError("The reviewed library file changed.")
            await record_removal(db, plan)
        except Exception:
            await asyncio.to_thread(prune_workspaces, plan)
            raise
        operation = str(plan.operation_id)
        item.before_state = json.dumps({**before, "removal_operation": operation})
        arc.diagnostics = {
            **(arc.diagnostics or {}),
            "pending_member_cleanup": {
                "operation": operation,
                "path": str(plan.source),
                "trash_path": str(dest) if dest else None,
            },
        }
        await db.commit()
    try:
        _, item = await _owner(db, config, item_id)
        _, binding = await _snapshot(
            db,
            review.arc_id,
            config["actor"],
            lock=True,
            cleanup_job=item.job_id,
            omit=frozenset(t["id"] for t in targets if "original" not in t),
            omit_files=frozenset(t["id"] for t in targets if "original" in t),
        )
        trash = await resolve_configured_utility_trash_dir(db)
        if binding != config["residual"] or (str(trash) if trash else None) != config["trash"]:
            raise StoryArcConflictError("The arc or Trash settings changed before removal.")
        await stage_member_removal(db, review, plan)
        await db.commit()
    except Exception:
        await db.rollback()
        # A failed commit response is ambiguous. Never start destructive cleanup
        # from this exception path; keep any committed detachment for explicit retry.
        if await recover_uncommitted_removal(db, plan.operation_id) == "abandoned":
            await _clear_marker(db, review.arc_id, operation)
        raise
    await complete_removal(db, plan.operation_id)
    await _clear_marker(db, review.arc_id, operation)
    return {
        "removed": str(plan.source),
        "trash_path": str(plan.trash_path) if plan.trash_path else None,
    }
