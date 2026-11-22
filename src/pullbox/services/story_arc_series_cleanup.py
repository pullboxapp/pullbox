"""Review and atomically remove selected parent records, never physical files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, or_, select, update

from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJobAction,
    ImportJobActionStatus,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, MatchConfidence
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcPlacement
from pullbox.models.story_arc_import import ImportedStoryArcEntry
from pullbox.services.download_operation_reconciliation import retire_download_operations
from pullbox.services.library_mutation_coordination import (
    lock_file_mutation_admission,
    require_no_archive_publication,
)
from pullbox.services.story_arc_deletion import ArcDeleteReview, _delete_arc_record, _snapshot
from pullbox.services.story_arc_member_review import resolved_member_issue_ids
from pullbox.services.story_arc_series_detachment import (
    check_series_arc_work,
    detach_series_arc_entries,
    reserve_series_arc_work,
)
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.job_queue_state import TERMINAL_STATES
from pullbox.utilities.models import UtilityJob

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _guard_retained_work(db: AsyncSession, ids: list[int], *, lock: bool) -> None:
    issues = select(Issue.id).where(Issue.series_id.in_(ids))
    files = select(LibraryFile.id).where(LibraryFile.issue_id.in_(issues))
    paths = select(LibraryFile.file_path).where(LibraryFile.issue_id.in_(issues))
    action = ImportJobAction.payload
    retained = (
        select(ImportedSeries.id).where(ImportedSeries.series_id.in_(ids)),
        select(ImportedStoryArcEntry.id).where(ImportedStoryArcEntry.matched_issue_id.in_(issues)),
        select(ImportedFile.id).where(
            or_(
                ImportedFile.library_file_id.in_(files),
                ImportedFile.file_path.in_(paths),
                ImportedFile.matched_issue_id.in_(issues),
            )
        ),
        select(ImportJobAction.id).where(
            ImportJobAction.status != ImportJobActionStatus.ROLLED_BACK,
            or_(
                action["series_id"].as_integer().in_(ids),
                action["issue_id"].as_integer().in_(issues),
                action["library_file_id"].as_integer().in_(files),
                action["destination_path"].as_string().in_(paths),
            ),
        ),
    )
    if await db.scalar(select(or_(*(q.exists() for q in retained)))):
        raise StoryArcConflictError(
            "A selected series has import or rollback evidence. "
            "Keep that series and review its import history."
        )
    active = (
        (
            DownloadHistory,
            or_(
                DownloadHistory.state.not_in(
                    (DownloadState.FAILED, DownloadState.COMPLETED, DownloadState.IMPORTED)
                ),
                (DownloadHistory.state == DownloadState.COMPLETED)
                & DownloadHistory.imported_at.is_(None),
            ),
        ),
        (
            DirectAcquisitionAttempt,
            DirectAcquisitionAttempt.state.not_in(
                (
                    DirectAcquisitionState.CANCELLED,
                    DirectAcquisitionState.COMPLETED,
                    DirectAcquisitionState.FAILED,
                )
            ),
        ),
        (PendingMatch, PendingMatch.status == PendingMatchStatus.PENDING),
    )
    for model, predicate in active:
        query = select(model.id).where(model.issue_id.in_(issues))
        if lock and db.get_bind().dialect.name == "postgresql":
            await db.execute(query.order_by(model.id).with_for_update())
        if await db.scalar(query.where(predicate).limit(1)):
            raise StoryArcConflictError(
                "A selected issue has acquisition work. "
                "Finish or resolve it before deleting its series."
            )


@dataclass(frozen=True)
class SeriesFileProgress:
    """Normalize only changes made by completed, ordered items of this job."""

    files: dict[int, tuple[int, str, int]]
    removed: frozenset[int]
    placements: frozenset[int]
    pending_placements: frozenset[int]
    issue_states: dict[int, tuple[str, bool, str, bool]]
    operation: str | None

    @classmethod
    def from_items(
        cls, targets: list[dict[str, Any]], completed: int, operation: str | None
    ) -> SeriesFileProgress:
        removed: set[int] = set()
        states = {}
        files = {}
        for i, target in enumerate(targets):
            if "original" not in target:
                continue
            original = target["original"]
            iid, fid = original["issue_id"], original["file"]["id"]
            files[fid] = iid, target["file_path"], original["file"]["root_id"]
            if i < completed:
                removed.add(fid)
                owns = any(c["id"] not in removed for c in original["remaining"])
                status, skip = target["initial_issue"]
                states[iid] = (status, skip, "owned" if owns else "skipped", skip if owns else True)
        return cls(
            files,
            frozenset(removed),
            frozenset(t["id"] for t in targets if "original" not in t),
            frozenset(t["id"] for t in targets[completed:] if "original" not in t),
            states,
            operation,
        )


def series_binding(plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({**plan, "previous_job": None}, sort_keys=True).encode()
    ).hexdigest()


async def series_record_plan(
    db: AsyncSession,
    arc_id: int,
    actor: int,
    ids: list[int],
    *,
    lock: bool,
    job_id: str | None = None,
    progress: SeriesFileProgress | None = None,
) -> dict[str, Any]:
    if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids) or min(ids) < 1:
        raise StoryArcConflictError("Select between 1 and 100 distinct related series.")
    if lock:
        await lock_file_mutation_admission(db)
        await ensure_no_active_import_file_mutation(db)
        await reserve_series_arc_work(db, ids, deleting_arc_id=arc_id)
    else:
        await check_series_arc_work(db, ids, deleting_arc_id=arc_id)
    review, residual = await _snapshot(
        db,
        arc_id,
        actor,
        lock=lock,
        cleanup_job=job_id,
        omit=progress.placements if progress else frozenset(),
        omit_files=frozenset(progress.files) if progress else frozenset(),
    )
    if progress and progress.placements:
        pending = await db.scalars(
            select(StoryArcPlacement.id)
            .join(IssueStoryArc)
            .where(
                IssueStoryArc.story_arc_id == arc_id, StoryArcPlacement.id.in_(progress.placements)
            )
        )
        if set(pending) != progress.pending_placements:
            raise StoryArcConflictError("The selected arc copies changed. Review remaining files.")
    if review.blocker:
        raise StoryArcConflictError(review.blocker)
    arc = await db.get(StoryArc, arc_id)
    assert arc is not None
    receipt = (arc.diagnostics or {}).get("pending_member_cleanup") or {}
    if receipt and not (
        progress
        and job_id
        and progress.operation
        and receipt.get("operation") == progress.operation
    ):
        raise StoryArcConflictError(
            "Finish this arc's interrupted file cleanup before deleting series."
        )
    members = resolved_member_issue_ids(arc_id)
    series = (
        await db.execute(
            select(Series.id, Series.title, Series.created_at, Series.path, Series.year_start)
            .where(
                Series.id.in_(ids),
                Series.id.in_(select(Issue.series_id).where(Issue.id.in_(members))),
            )
            .order_by(Series.id)
        )
    ).all()
    if len(series) != len(ids):
        raise StoryArcConflictError(
            "Only series with a confirmed member of this arc may be selected."
        )
    await _guard_retained_work(db, ids, lock=lock)
    records = {s.id: dict(id=s.id, title=s.title, issues=0, outside=0, files=0) for s in series}
    seal = hashlib.sha256()

    def bind(row: tuple[Any, ...]) -> None:
        seal.update(json.dumps(row, default=str).encode() + b"\n")

    for s in series:
        bind(tuple(s))
    for issue in await db.execute(
        select(
            Issue.series_id,
            Issue.id,
            Issue.created_at,
            Issue.issue_number,
            Issue.issue_number_text,
            Issue.status,
            Issue.title,
            Issue.id.in_(members).label("member"),
            Issue.manual_skip,
        )
        .where(Issue.series_id.in_(ids))
        .order_by(Issue.id)
    ):
        if issue.status == IssueStatus.DOWNLOADING:
            raise StoryArcConflictError(
                "A selected issue is downloading. Finish or cancel its download first."
            )
        value = list(issue)
        if progress:
            restore = progress.issue_states.get(issue.id)
            if restore:
                status, skip, expected_status, expected_skip = restore
                if (issue.status.value, issue.manual_skip) != (expected_status, expected_skip):
                    raise StoryArcConflictError(
                        "Issue ownership changed during cleanup. Review again."
                    )
                value[5], value[-1] = status, skip
        else:
            value.pop()  # Keep existing record-only job signatures compatible.
        bind(tuple(value))
        records[issue.series_id]["issues"] += 1
        records[issue.series_id]["outside"] += int(not issue.member)
    paths = []
    outstanding: set[int] = set()
    for f in await db.execute(
        select(
            Issue.series_id,
            LibraryFile.id,
            LibraryFile.issue_id,
            LibraryFile.library_root_id,
            LibraryFile.file_path,
            LibraryFile.storage_mode,
            LibraryFile.created_at,
        )
        .join(Issue, Issue.id == LibraryFile.issue_id)
        .where(Issue.series_id.in_(ids))
        .order_by(LibraryFile.id)
    ):
        if progress and f.id in progress.files:
            if (
                f.id in progress.removed
                or progress.files[f.id] != (f.issue_id, f.file_path, f.library_root_id)
                or f.storage_mode != LibraryFileStorageMode.MANAGED
            ):
                raise StoryArcConflictError("A selected file registration changed. Review again.")
            outstanding.add(f.id)
            if f.file_path != receipt.get("path"):
                paths.append(Path(f.file_path))
            continue
        bind(tuple(f))
        records[f.series_id]["files"] += 1
        paths.append(Path(f.file_path))
    if progress and outstanding != progress.files.keys() - progress.removed:
        raise StoryArcConflictError("A selected series file is no longer registered. Review again.")
    if paths:
        await require_no_archive_publication(db, *paths, include_descendants=False)
    busy = (
        select(UtilityJob.id)
        .where(
            UtilityJob.id == StoryArc.diagnostics["reviewed_cleanup"].as_string(),
            UtilityJob.state.not_in(TERMINAL_STATES),
        )
        .correlate(StoryArc)
        .exists()
    )
    links = (
        select(
            StoryArc.id,
            StoryArc.name,
            StoryArc.revision,
            StoryArc.lifecycle,
            IssueStoryArc.id.label("entry"),
            IssueStoryArc.issue_id,
            IssueStoryArc.resolution_state,
            busy.label("cleaning"),
            StoryArc.diagnostics["pending_member_cleanup"]["operation"]
            .as_string()
            .label("receipt"),
        )
        .select_from(Issue)
        .join(IssueStoryArc, IssueStoryArc.issue_id == Issue.id)
        .join(StoryArc, StoryArc.id == IssueStoryArc.story_arc_id)
        .where(Issue.series_id.in_(ids), StoryArc.id != arc_id)
        .order_by(StoryArc.id, IssueStoryArc.id)
    )
    peers: dict[int, dict[str, Any]] = {}
    for peer in await db.execute(links.with_for_update(of=StoryArc) if lock else links):
        if peer.cleaning or peer.receipt:
            raise StoryArcConflictError(
                f'Finish or recover file cleanup for "{peer.name}" before deleting these series.'
            )
        bind(tuple(peer))
        peers.setdefault(peer.id, dict(id=peer.id, name=peer.name, linked=0))["linked"] += 1
    return dict(
        arc_id=arc_id,
        name=arc.name,
        actor=actor,
        impact=asdict(review),
        residual=residual,
        previous_job=(arc.diagnostics or {}).get("reviewed_cleanup"),
        evidence=seal.hexdigest(),
        record_series=list(records.values()),
        shared_arcs=list(peers.values()),
    )


async def check_series_item(db: AsyncSession, arc_id: int, expected: dict[str, Any]) -> None:
    """Check the reviewed catalog totals before reporting progress for this series."""
    issues = select(Issue.id).where(Issue.series_id == expected["id"])
    count, members = (
        await db.execute(
            select(
                func.count(Issue.id),
                func.count(Issue.id).filter(Issue.id.in_(resolved_member_issue_ids(arc_id))),
            ).where(Issue.series_id == expected["id"])
        )
    ).one()
    files = await db.scalar(
        select(func.count(LibraryFile.id)).where(LibraryFile.issue_id.in_(issues))
    )
    if not members or (count, count - members, files) != (
        expected["issues"],
        expected["outside"],
        expected["files"],
    ):
        raise StoryArcConflictError("This catalog changed. No records were removed. Review again.")


async def verify_series_progress(
    db: AsyncSession,
    plan: dict[str, Any],
    job_id: str,
    completed: int,
    operation: str | None = None,
) -> dict[str, Any]:
    projection = SeriesFileProgress.from_items(plan["targets"], completed, operation)
    current = await series_record_plan(
        db,
        plan["arc_id"],
        plan["actor"],
        [s["id"] for s in plan["record_series"]],
        lock=True,
        job_id=job_id,
        progress=projection,
    )
    if current["previous_job"] != job_id or series_binding(current) != plan["series_file_binding"]:
        raise StoryArcConflictError(
            "The selected series or reading orders changed. Review the remaining files."
        )
    return current


async def finish_series_records(db: AsyncSession, plan: dict[str, Any], job_id: str) -> None:
    ids = [s["id"] for s in plan["record_series"]]
    arc_id, actor = plan["arc_id"], plan["actor"]
    targets = plan.get("targets", ())
    removed = len(targets)
    if removed:
        current = await verify_series_progress(db, plan, job_id, removed)
    else:
        current = await series_record_plan(db, arc_id, actor, ids, lock=True, job_id=job_id)
        if current != {**plan, "previous_job": job_id}:
            raise StoryArcConflictError(
                "The selected series or another reading order changed. "
                "No records were removed. Review again."
            )
    # The job's cancellation lock and this transaction cover the whole selection.
    # Release only this arc first; detachment must retain chapters in surviving arcs.
    await _delete_arc_record(
        db,
        arc_id,
        actor,
        ArcDeleteReview(**current["impact"]),
        job_id=job_id,
        series_ids=ids,
        removed_series_files=sum("original" in t for t in targets),
        removed_arc_files=sum("original" not in t for t in targets),
    )
    issues = select(Issue.id).where(Issue.series_id.in_(ids))
    for did in await db.scalars(
        select(DownloadHistory.id).where(DownloadHistory.issue_id.in_(issues))
    ):
        await retire_download_operations(db, did)
    for sid in ids:
        parent = await db.get(Series, sid)
        assert parent is not None
        await detach_series_arc_entries(db, parent)
    await db.execute(
        update(LibraryFile)
        .where(LibraryFile.issue_id.in_(issues))
        .values(issue_id=None, match_confidence=MatchConfidence.UNMATCHED)
    )
    await db.execute(delete(Series).where(Series.id.in_(ids)))
