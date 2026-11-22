"""Explicit selection and durable execution of managed arc-file cleanup."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import false, select, update

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcResolutionState,
)
from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir
from pullbox.services.story_arc_deletion import _delete_arc_record, _snapshot
from pullbox.services.story_arc_member_removal import review_member_removal
from pullbox.services.story_arc_placement_integration import StoryArcPlacementSyncService
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, JobType, UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.utilities.job_queue import JobQueueManager


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt="story-arc-selected-cleanup-v1")


def _target(p: StoryArcPlacement) -> dict[str, Any]:
    return {
        "id": p.id,
        "file_path": p.placement_path,
        "member": p.issue_story_arc_id,
        "proof": _digest(
            [
                p.mode,
                p.ownership,
                p.symlink_style,
                p.library_root_id,
                p.library_file_id,
                p.creating_action_id,
                p.source_import_job_id,
                p.source_fingerprint,
                (p.last_result or {}).get("target_fingerprint"),
            ]
        ),
    }


async def _plan(
    db: AsyncSession, arc_id: int, actor: int, ids: list[int], *, member_ids: list[int], lock: bool
) -> dict[str, Any]:
    selected = frozenset(ids)
    originals = frozenset(member_ids)
    if (
        not (1 <= len(ids) + len(member_ids) <= 1000)
        or len(selected) != len(ids)
        or len(originals) != len(member_ids)
    ):
        raise StoryArcConflictError("Select between 1 and 1,000 different files.")
    review, residual = await _snapshot(
        db, arc_id, actor, lock=lock, omit=selected, omit_files=originals
    )
    if review.blocker:
        raise StoryArcConflictError(review.blocker)
    arc = await db.get(StoryArc, arc_id)
    assert arc is not None
    q = (
        select(StoryArcPlacement)
        .join(IssueStoryArc)
        .where(
            IssueStoryArc.story_arc_id == arc_id,
            StoryArcPlacement.id.in_(selected),
            StoryArcPlacement.ownership == "managed",
            ~select(LibraryFile.id)
            .where(LibraryFile.file_path == StoryArcPlacement.placement_path)
            .exists(),
        )
        .order_by(StoryArcPlacement.id)
    )
    rows = (await db.scalars(q.execution_options(populate_existing=True))).all()
    if len(rows) != len(selected) or any(
        not p.source_fingerprint or not (p.last_result or {}).get("target_fingerprint")
        for p in rows
    ):
        raise StoryArcConflictError(
            "Select only proven managed arc files, not references or library files."
        )
    trash = await resolve_configured_utility_trash_dir(db)
    targets = [_target(p) for p in rows]
    peers: dict[int, str] = {}
    skipped: set[int] = set()
    effects: dict[int, dict[str, Any]] = {}
    evidence_bytes = 0
    for fid in sorted(originals):
        others = (
            await db.execute(
                select(StoryArc.id, StoryArc.name)
                .join(IssueStoryArc)
                .join(LibraryFile, LibraryFile.issue_id == IssueStoryArc.issue_id)
                .where(
                    LibraryFile.id == fid,
                    StoryArc.id != arc_id,
                    IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                )
                .order_by(StoryArc.id)
            )
        ).all()
        file = await review_member_removal(
            db, arc_id, fid, shared_arcs=[r.id for r in others], lock=lock
        )
        peers.update({r.id: r.name for r in others})
        remaining = sum(c.id not in originals for c in file.remaining)
        if not remaining:
            skipped.add(file.issue_id)
        if file.issue_id not in effects:
            title, number, fallback = (
                await db.execute(
                    select(Series.title, Issue.issue_number_text, Issue.issue_number)
                    .join(Issue, Issue.series_id == Series.id)
                    .where(Issue.id == file.issue_id)
                )
            ).one()
            effects[file.issue_id] = {
                "id": file.issue_id,
                "label": f"{title} #{number or format_issue_number(fallback)}",
                "remaining": remaining,
            }
        encoded = file.model_dump_json()
        evidence_bytes += len(encoded.encode())
        if evidence_bytes > 2 * 1024 * 1024:
            raise StoryArcConflictError(
                "This selection has too much file evidence. Review fewer library files at a time."
            )
        targets.append(
            {"id": fid, "file_path": str(file.file.path), "original": json.loads(encoded)}
        )
    return {
        "arc_id": arc_id,
        "actor": actor,
        "revision": arc.revision,
        "name": arc.name,
        "previous_job": (arc.diagnostics or {}).get("reviewed_cleanup"),
        "impact": {
            **asdict(review),
            "files": review.files + len(originals),
            "placements": review.placements + len(selected),
            "managed_placements": review.managed_placements + len(selected),
        },
        "residual": residual,
        "trash": str(trash) if trash else None,
        "shared_arcs": [{"id": i, "name": peers[i]} for i in sorted(peers)],
        "skipped_issues": len(skipped),
        "issue_effects": list(effects.values()),
        "targets": targets,
    }


async def preview_cleanup(
    db: AsyncSession,
    arc_id: int,
    actor: int,
    ids: list[int],
    *,
    member_ids: list[int] | None = None,
) -> dict[str, Any]:
    plan = await _plan(db, arc_id, actor, ids, member_ids=member_ids or [], lock=False)
    return {
        "paths": [p["file_path"] for p in plan["targets"]],
        "impact": plan["impact"],
        "shared_arcs": plan["shared_arcs"],
        "skipped_issues": plan["skipped_issues"],
        "issue_effects": plan["issue_effects"],
        "trash_enabled": plan["trash"] is not None,
        "token": _signer().dumps(_digest(plan)),
    }


async def enqueue_reviewed_cleanup(
    db: AsyncSession,
    manager: JobQueueManager,
    arc_id: int,
    actor: int,
    username: str,
    ids: list[int],
    token: str,
    *,
    member_ids: list[int] | None = None,
    allow_shared: bool = False,
) -> UtilityJob:
    plan = await _plan(db, arc_id, actor, ids, member_ids=member_ids or [], lock=True)
    if plan["shared_arcs"] and not allow_shared:
        raise StoryArcConflictError("Confirm the shared comic impact before removing these files.")
    try:
        valid = _signer().loads(token, max_age=900) == _digest(plan)
    except BadSignature:
        valid = False
    if not valid:
        raise StoryArcConflictError(
            "The selected files, arc or Trash settings changed. Review cleanup again."
        )
    await ensure_no_active_import_file_mutation(db)
    job = await manager.create_job(
        db, JobType.STORY_ARC_CLEANUP, "Delete Story Arc: " + plan["name"], plan, username
    )
    arc = await db.get(StoryArc, arc_id)
    assert arc is not None
    arc.diagnostics = {**(arc.diagnostics or {}), "reviewed_cleanup": job.id}
    await db.flush()
    return job


async def remove_reviewed_target(
    db: AsyncSession, plan: dict[str, Any], target: dict[str, Any], item_id: str
) -> dict[str, Any]:
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(update(StoryArc).where(false()).values(revision=StoryArc.revision))
    arc = await db.scalar(select(StoryArc).where(StoryArc.id == plan["arc_id"]).with_for_update())
    job_id = await db.scalar(select(UtilityJobItem.job_id).where(UtilityJobItem.id == item_id))
    if (
        not arc
        or arc.revision != plan["revision"]
        or not job_id
        or (arc.diagnostics or {}).get("reviewed_cleanup") != job_id
    ):
        raise StoryArcConflictError("This arc changed. Review the remaining files again.")
    state = await db.scalar(
        select(UtilityJob.state).where(UtilityJob.id == job_id).with_for_update()
    )
    if state != JobState.RUNNING:
        raise StoryArcConflictError("Cleanup stopped before starting this file.")
    await ensure_no_active_import_file_mutation(db)
    member = await db.scalar(
        select(IssueStoryArc)
        .where(IssueStoryArc.id == target["member"], IssueStoryArc.story_arc_id == arc.id)
        .with_for_update()
    )
    placement = await db.scalar(
        select(StoryArcPlacement).where(StoryArcPlacement.id == target["id"]).with_for_update()
    )
    if member and placement is None:
        receipt = member.last_materialization_result or {}
        if (
            receipt.get("cleanup_item_id") == item_id
            and receipt.get("placement_id") == target["id"]
        ):
            return dict(receipt)
    if not member or placement is None or _target(placement) != target:
        raise StoryArcConflictError("The reviewed arc file changed. Its path was left alone.")
    result = await StoryArcPlacementSyncService().remove_placement(
        db,
        arc.id,
        placement.id,
        confirm_managed_artifact_removal=True,
        expected_trash_enabled=plan["trash"] is not None,
        expected_trash_root=plan["trash"],
        cleanup_item_id=item_id,
    )
    return {
        "artifact_removed": result.artifact_removed,
        "canonical_preserved": True,
        "trash_path": str(result.trash_path) if result.trash_path else None,
    }


async def finish_reviewed_cleanup(db: AsyncSession, job: UtilityJob, plan: dict[str, Any]) -> None:
    review, binding = await _snapshot(
        db, plan["arc_id"], plan["actor"], lock=True, cleanup_job=job.id
    )
    arc = await db.get(StoryArc, plan["arc_id"])
    if (
        not arc
        or (arc.diagnostics or {}).get("reviewed_cleanup") != job.id
        or (arc.diagnostics or {}).get("pending_member_cleanup")
        or review.blocker
        or binding != plan["residual"]
    ):
        raise StoryArcConflictError(
            "The arc changed during cleanup. Review it again; its record was kept."
        )
    await _delete_arc_record(db, arc.id, plan["actor"], review, job_id=job.id)
