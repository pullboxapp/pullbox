"""Review and atomically remove selected parent records, never physical files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc
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


async def series_record_plan(
    db: AsyncSession,
    arc_id: int,
    actor: int,
    ids: list[int],
    *,
    lock: bool,
    job_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids) or min(ids) < 1:
        raise StoryArcConflictError("Select between 1 and 100 distinct related series.")
    if lock:
        await lock_file_mutation_admission(db)
        await ensure_no_active_import_file_mutation(db)
        await reserve_series_arc_work(db, ids)
    else:
        await check_series_arc_work(db, ids)
    review, residual = await _snapshot(db, arc_id, actor, lock=lock, cleanup_job=job_id)
    if review.blocker:
        raise StoryArcConflictError(review.blocker)
    arc = await db.get(StoryArc, arc_id)
    assert arc is not None
    if (arc.diagnostics or {}).get("pending_member_cleanup"):
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
        )
        .where(Issue.series_id.in_(ids))
        .order_by(Issue.id)
    ):
        if issue.status == IssueStatus.DOWNLOADING:
            raise StoryArcConflictError(
                "A selected issue is downloading. Finish or cancel its download first."
            )
        bind(tuple(issue))
        records[issue.series_id]["issues"] += 1
        records[issue.series_id]["outside"] += int(not issue.member)
    paths = []
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
        bind(tuple(f))
        records[f.series_id]["files"] += 1
        paths.append(Path(f.file_path))
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


async def finish_series_records(db: AsyncSession, plan: dict[str, Any], job_id: str) -> None:
    ids = [s["id"] for s in plan["record_series"]]
    arc_id, actor = plan["arc_id"], plan["actor"]
    current = await series_record_plan(db, arc_id, actor, ids, lock=True, job_id=job_id)
    if current != {**plan, "previous_job": job_id}:
        raise StoryArcConflictError(
            "The selected series or another reading order changed. "
            "No records were removed. Review again."
        )
    # The job's cancellation lock and this transaction cover the whole selection.
    # Release only this arc first; detachment must retain chapters in surviving arcs.
    await _delete_arc_record(
        db, arc_id, actor, ArcDeleteReview(**current["impact"]), job_id=job_id, series_ids=ids
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
