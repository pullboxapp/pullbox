"""Step 3 review queries and decisions for staged story arcs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
)
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJob, ImportJobStatus
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_story_arc_policy_confirmation import (
    ImportStoryArcPolicyReview,
    build_import_story_arc_policy_review,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class StoryArcMergeCandidate:
    """One bounded exact-name target offered for an explicit merge."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ImportedStoryArcReviewRow:
    """Bounded Step 3 presentation row for one staged story arc."""

    id: int
    name: str
    source_kind: StoryArcSourceKind
    source_ordinal: int
    status: ImportedStoryArcStatus
    selected_for_import: bool
    proposed_story_arc_id: int | None
    proposed_story_arc_name: str | None
    merge_candidates: tuple[StoryArcMergeCandidate, ...]
    entries_total: int
    entries_resolved: int
    entries_missing: int
    entries_ambiguous: int
    entries_conflict: int
    entries_pending: int
    entries_skipped: int
    selection_blocked: bool
    selection_block_reason: str | None
    policy_review: ImportStoryArcPolicyReview


@dataclass(frozen=True, slots=True)
class ImportedStoryArcReviewPage:
    """Paginated staged-arc review results."""

    items: tuple[ImportedStoryArcReviewRow, ...]
    total: int
    page: int
    page_size: int


StoryArcReviewAction = Literal["select", "skip"]
StoryArcDecisionTuple = tuple[int, StoryArcReviewAction, int | None]


async def auto_confirm_trusted_logical_story_arcs(
    session: AsyncSession,
    job_id: int,
    *,
    batch_size: int = 100,
) -> int:
    """Confirm exact local arc evidence for logical post-import creation only."""
    if isinstance(batch_size, bool) or batch_size <= 0:
        raise ValidationError("Story arc auto-confirmation batch size must be positive")

    confirmed = 0
    last_id = 0
    while True:
        arcs = list(
            (
                await session.scalars(
                    select(ImportedStoryArc)
                    .where(
                        ImportedStoryArc.import_job_id == job_id,
                        ImportedStoryArc.id > last_id,
                        ImportedStoryArc.status.in_(
                            (
                                ImportedStoryArcStatus.DETECTED,
                                ImportedStoryArcStatus.NEEDS_REVIEW,
                            )
                        ),
                        ImportedStoryArc.selected_for_import.is_(False),
                    )
                    .options(selectinload(ImportedStoryArc.entries))
                    .order_by(ImportedStoryArc.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not arcs:
            break

        blocked_arc_ids = await _load_arc_ids_with_current_safety(
            session,
            [int(arc.id) for arc in arcs],
        )
        for arc in arcs:
            if int(arc.id) in blocked_arc_ids or not _has_trusted_complete_arc_evidence(arc):
                continue
            arc.proposed_policy_snapshot = _logical_auto_policy(arc.source_kind)
            diagnostics = dict(arc.diagnostics or {})
            diagnostics["auto_confirmation"] = {
                "schema_version": 1,
                "scope": "logical_membership_only",
                "evidence": "trusted_local_exact_identity",
            }
            arc.diagnostics = diagnostics
            arc.status = ImportedStoryArcStatus.CONFIRMED
            arc.selected_for_import = True
            for entry in arc.entries:
                entry.selected_for_import = (
                    entry.resolution_state != StoryArcResolutionState.SKIPPED
                )
            confirmed += 1
        await session.flush()
        last_id = int(arcs[-1].id)
    return confirmed


async def load_import_story_arc_review_page(
    session: AsyncSession,
    job_id: int,
    *,
    page: int = 1,
    page_size: int = 25,
) -> ImportedStoryArcReviewPage:
    """Return one deterministic, bounded page of staged story arcs."""
    if page < 1:
        raise ValidationError("Story arc review page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise ValidationError("Story arc review page_size must be between 1 and 100")

    await _require_review_job(session, job_id)
    total = int(
        await session.scalar(
            select(func.count(ImportedStoryArc.id)).where(ImportedStoryArc.import_job_id == job_id)
        )
        or 0
    )
    arcs = list(
        (
            await session.execute(
                select(ImportedStoryArc)
                .where(ImportedStoryArc.import_job_id == job_id)
                .order_by(ImportedStoryArc.source_ordinal.asc(), ImportedStoryArc.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    arc_ids = [int(arc.id) for arc in arcs]
    counts_by_arc_id = await _load_entry_counts(session, arc_ids)
    safety_blocked_arc_ids = await _load_arc_ids_with_current_safety(session, arc_ids)
    candidates_by_arc_id = await _load_merge_candidates(session, arcs)

    rows: list[ImportedStoryArcReviewRow] = []
    for arc in arcs:
        counts = counts_by_arc_id.get(int(arc.id), {})
        conflict_count = counts.get(StoryArcResolutionState.CONFLICT, 0)
        safety_blocked = int(arc.id) in safety_blocked_arc_ids
        block_reason: str | None = None
        if safety_blocked:
            block_reason = "Resolve safety findings before selecting this story arc."
        elif conflict_count:
            block_reason = "Resolve or skip conflict entries before selecting this story arc."

        candidates = candidates_by_arc_id.get(int(arc.id), ())
        proposed_name = next(
            (
                candidate.name
                for candidate in candidates
                if candidate.id == arc.proposed_story_arc_id
            ),
            None,
        )
        rows.append(
            ImportedStoryArcReviewRow(
                id=int(arc.id),
                name=arc.name or "Unnamed story arc",
                source_kind=arc.source_kind,
                source_ordinal=int(arc.source_ordinal),
                status=arc.status,
                selected_for_import=bool(arc.selected_for_import),
                proposed_story_arc_id=arc.proposed_story_arc_id,
                proposed_story_arc_name=proposed_name,
                merge_candidates=candidates,
                entries_total=sum(counts.values()),
                entries_resolved=counts.get(StoryArcResolutionState.RESOLVED, 0),
                entries_missing=counts.get(StoryArcResolutionState.MISSING, 0),
                entries_ambiguous=counts.get(StoryArcResolutionState.AMBIGUOUS, 0),
                entries_conflict=conflict_count,
                entries_pending=counts.get(StoryArcResolutionState.PENDING, 0),
                entries_skipped=counts.get(StoryArcResolutionState.SKIPPED, 0),
                selection_blocked=bool(block_reason),
                selection_block_reason=block_reason,
                policy_review=build_import_story_arc_policy_review(
                    arc.proposed_policy_snapshot or {},
                    arc.source_settings_snapshot or {},
                    arc.diagnostics or {},
                ),
            )
        )

    return ImportedStoryArcReviewPage(
        items=tuple(rows),
        total=total,
        page=page,
        page_size=page_size,
    )


async def update_import_story_arc_decision(
    session: AsyncSession,
    job_id: int,
    imported_story_arc_id: int,
    *,
    action: StoryArcReviewAction,
    proposed_story_arc_id: int | None,
) -> ImportedStoryArc:
    """Persist one explicit select/skip decision without creating canonical rows."""
    await _require_review_job(session, job_id)
    staged_arc = await session.get(ImportedStoryArc, imported_story_arc_id)
    if staged_arc is None or staged_arc.import_job_id != job_id:
        raise NotFoundError("ImportedStoryArc", imported_story_arc_id)
    if action not in {"select", "skip"}:
        raise ValidationError("Story arc review action must be select or skip")

    entries = list(
        (
            await session.execute(
                select(ImportedStoryArcEntry)
                .where(ImportedStoryArcEntry.imported_story_arc_id == staged_arc.id)
                .order_by(
                    ImportedStoryArcEntry.source_ordinal.asc(),
                    ImportedStoryArcEntry.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    if action == "skip":
        if proposed_story_arc_id is not None:
            raise ValidationError("A skipped story arc cannot have a proposed merge target")
        staged_arc.status = ImportedStoryArcStatus.SKIPPED
        staged_arc.selected_for_import = False
        staged_arc.proposed_story_arc_id = None
        for entry in entries:
            entry.selected_for_import = False
        await session.flush()
        return staged_arc

    await _validate_merge_target(session, proposed_story_arc_id)
    safety_blocked_arc_ids = await _load_arc_ids_with_current_safety(
        session,
        [int(staged_arc.id)],
    )
    _assert_story_arc_selectable(
        entries,
        has_current_safety_block=int(staged_arc.id) in safety_blocked_arc_ids,
    )
    staged_arc.status = ImportedStoryArcStatus.READY
    staged_arc.selected_for_import = True
    staged_arc.proposed_story_arc_id = proposed_story_arc_id
    for entry in entries:
        entry.selected_for_import = entry.resolution_state != StoryArcResolutionState.SKIPPED
    await session.flush()
    return staged_arc


async def confirm_import_story_arcs(
    session: AsyncSession,
    job_id: int,
    *,
    story_arc_ids: Sequence[int],
    decisions: Sequence[StoryArcDecisionTuple],
    batch_size: int = 250,
) -> int:
    """Apply decisions and confirm selected arcs through bounded keyset pages."""
    if isinstance(batch_size, bool) or batch_size <= 0:
        raise ValidationError("Story arc confirmation batch size must be positive")
    await _require_review_job(session, job_id)
    normalized_decisions = [
        (int(arc_id), action, proposed_story_arc_id)
        for arc_id, action, proposed_story_arc_id in decisions
    ]
    decision_ids = {arc_id for arc_id, _, _ in normalized_decisions}
    compatibility_ids = list(dict.fromkeys(int(value) for value in story_arc_ids))
    requested_ids = decision_ids.union(compatibility_ids)
    arcs_by_id: dict[int, ImportedStoryArc] = {}
    requested_id_list = sorted(requested_ids)
    for start in range(0, len(requested_id_list), batch_size):
        page_ids = requested_id_list[start : start + batch_size]
        requested_result = await session.execute(
            select(ImportedStoryArc)
            .where(
                ImportedStoryArc.import_job_id == job_id,
                ImportedStoryArc.id.in_(page_ids),
            )
            .options(selectinload(ImportedStoryArc.entries))
            .order_by(ImportedStoryArc.id)
        )
        arcs_by_id.update(
            {int(staged_arc.id): staged_arc for staged_arc in requested_result.scalars().all()}
        )
    for requested_id in requested_ids:
        if requested_id not in arcs_by_id:
            raise NotFoundError("ImportedStoryArc", requested_id)

    target_ids = {
        int(target_id)
        for _, action, target_id in normalized_decisions
        if action == "select" and target_id is not None
    }
    target_ids.update(
        int(staged_arc.proposed_story_arc_id)
        for staged_arc in arcs_by_id.values()
        if staged_arc.proposed_story_arc_id is not None
    )
    merge_targets: dict[int, StoryArc] = {}
    await _load_merge_targets_by_id(session, target_ids, merge_targets)
    safety_blocked_arc_ids = await _load_arc_ids_with_current_safety(
        session,
        requested_id_list,
    )

    for arc_id, action, proposed_story_arc_id in normalized_decisions:
        _apply_loaded_story_arc_decision(
            arcs_by_id[arc_id],
            action=action,
            proposed_story_arc_id=proposed_story_arc_id,
            merge_targets=merge_targets,
            has_current_safety_block=arc_id in safety_blocked_arc_ids,
        )

    for arc_id in compatibility_ids:
        if arc_id in decision_ids:
            continue
        staged_arc = arcs_by_id[arc_id]
        _apply_loaded_story_arc_decision(
            staged_arc,
            action="select",
            proposed_story_arc_id=staged_arc.proposed_story_arc_id,
            merge_targets=merge_targets,
            has_current_safety_block=arc_id in safety_blocked_arc_ids,
        )

    confirmed_count = 0
    last_id = 0
    while True:
        selected_result = await session.execute(
            select(ImportedStoryArc)
            .where(
                ImportedStoryArc.import_job_id == job_id,
                ImportedStoryArc.selected_for_import.is_(True),
                ImportedStoryArc.id > last_id,
            )
            .options(selectinload(ImportedStoryArc.entries))
            .order_by(ImportedStoryArc.id)
            .limit(batch_size)
        )
        selected_arcs = list(selected_result.scalars().all())
        if not selected_arcs:
            break
        page_target_ids = {
            int(staged_arc.proposed_story_arc_id)
            for staged_arc in selected_arcs
            if staged_arc.proposed_story_arc_id is not None
        }
        await _load_merge_targets_by_id(session, page_target_ids, merge_targets)
        page_safety_blocked_arc_ids = await _load_arc_ids_with_current_safety(
            session,
            [int(staged_arc.id) for staged_arc in selected_arcs],
        )
        for staged_arc in selected_arcs:
            _validate_loaded_merge_target(
                staged_arc.proposed_story_arc_id,
                merge_targets,
            )
            _assert_story_arc_selectable(
                staged_arc.entries,
                has_current_safety_block=(int(staged_arc.id) in page_safety_blocked_arc_ids),
            )
            if staged_arc.status != ImportedStoryArcStatus.READY:
                raise ValidationError(
                    "Select each story arc from Step 3 before confirming the import"
                )
            staged_arc.status = ImportedStoryArcStatus.CONFIRMED
            staged_arc.selected_for_import = True
            confirmed_count += 1
        await session.flush()
        last_id = int(selected_arcs[-1].id)

    return confirmed_count


async def _load_merge_targets_by_id(
    session: AsyncSession,
    target_ids: set[int],
    loaded: dict[int, StoryArc],
) -> None:
    missing_ids = target_ids - loaded.keys()
    if not missing_ids:
        return
    loaded.update(
        {
            int(target.id): target
            for target in (
                await session.scalars(select(StoryArc).where(StoryArc.id.in_(missing_ids)))
            ).all()
        }
    )


def _apply_loaded_story_arc_decision(
    staged_arc: ImportedStoryArc,
    *,
    action: StoryArcReviewAction,
    proposed_story_arc_id: int | None,
    merge_targets: Mapping[int, StoryArc],
    has_current_safety_block: bool,
) -> None:
    if action not in {"select", "skip"}:
        raise ValidationError("Story arc review action must be select or skip")
    if action == "skip":
        if proposed_story_arc_id is not None:
            raise ValidationError("A skipped story arc cannot have a proposed merge target")
        staged_arc.status = ImportedStoryArcStatus.SKIPPED
        staged_arc.selected_for_import = False
        staged_arc.proposed_story_arc_id = None
        for entry in staged_arc.entries:
            entry.selected_for_import = False
        return

    _validate_loaded_merge_target(proposed_story_arc_id, merge_targets)
    _assert_story_arc_selectable(
        staged_arc.entries,
        has_current_safety_block=has_current_safety_block,
    )
    staged_arc.status = ImportedStoryArcStatus.READY
    staged_arc.selected_for_import = True
    staged_arc.proposed_story_arc_id = proposed_story_arc_id
    for entry in staged_arc.entries:
        entry.selected_for_import = entry.resolution_state != StoryArcResolutionState.SKIPPED


def _validate_loaded_merge_target(
    story_arc_id: int | None,
    merge_targets: Mapping[int, StoryArc],
) -> None:
    if story_arc_id is None:
        return
    target = merge_targets.get(story_arc_id)
    if target is None:
        raise NotFoundError("StoryArc", story_arc_id)
    if target.lifecycle == StoryArcLifecycle.ARCHIVED:
        raise ValidationError("An archived story arc cannot be selected as a merge target")


def _has_trusted_complete_arc_evidence(staged_arc: ImportedStoryArc) -> bool:
    if not staged_arc.name or not staged_arc.name.strip() or not staged_arc.entries:
        return False
    active_entries = [
        entry
        for entry in staged_arc.entries
        if entry.resolution_state != StoryArcResolutionState.SKIPPED
    ]
    if not active_entries or any(
        entry.resolution_state
        in {
            StoryArcResolutionState.PENDING,
            StoryArcResolutionState.AMBIGUOUS,
            StoryArcResolutionState.CONFLICT,
        }
        for entry in active_entries
    ):
        return False

    diagnostics = staged_arc.diagnostics or {}
    if staged_arc.source_kind == StoryArcSourceKind.MYLAR3:
        return diagnostics.get("source_name_present") is True and all(
            _is_provider_id(entry.source_issue_id) for entry in active_entries
        )
    if staged_arc.source_kind == StoryArcSourceKind.COMICINFO:
        return all(
            entry.resolution_state == StoryArcResolutionState.RESOLVED for entry in active_entries
        )
    if staged_arc.source_kind == StoryArcSourceKind.FOLDER:
        return diagnostics.get("reason") == "consistent_exact_arc_name" and all(
            entry.resolution_state == StoryArcResolutionState.RESOLVED
            and (entry.evidence or {}).get("has_comicinfo") is True
            for entry in active_entries
        )
    return False


def _is_provider_id(value: str | None) -> bool:
    return value is not None and value.strip().isdecimal() and int(value.strip()) > 0


def _logical_auto_policy(source_kind: StoryArcSourceKind) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": source_kind.value,
        "activation": "confirmed",
        "monitored": False,
        "search_missing": False,
        "include_upcoming": False,
        "sync_enabled": False,
        "placement_policy": {
            "schema_version": 1,
            "mode": "logical",
            "target_library_root_id": None,
            "destination_root": None,
            "folder_template": DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
            "file_template": DEFAULT_STORY_ARC_FILE_TEMPLATE,
            "symlink_style": None,
            "synchronize": False,
        },
    }


async def _require_review_job(session: AsyncSession, job_id: int) -> ImportJob:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update story arc decisions")
    return job


async def _validate_merge_target(session: AsyncSession, story_arc_id: int | None) -> None:
    if story_arc_id is None:
        return
    target = await session.get(StoryArc, story_arc_id)
    if target is None:
        raise NotFoundError("StoryArc", story_arc_id)
    if target.lifecycle == StoryArcLifecycle.ARCHIVED:
        raise ValidationError("An archived story arc cannot be selected as a merge target")


def _assert_story_arc_selectable(
    entries: Sequence[ImportedStoryArcEntry],
    *,
    has_current_safety_block: bool,
) -> None:
    if has_current_safety_block:
        raise ValidationError("Resolve story arc safety findings before confirming this arc")
    if any(entry.resolution_state == StoryArcResolutionState.CONFLICT for entry in entries):
        raise ValidationError(
            "Resolve or skip story arc conflict entries before confirming this arc"
        )


async def _load_arc_ids_with_current_safety(
    session: AsyncSession,
    arc_ids: Sequence[int],
) -> set[int]:
    """Return arcs whose currently linked import file is still safety blocked."""
    if not arc_ids:
        return set()
    return {
        int(arc_id)
        for arc_id in (
            await session.scalars(
                select(ImportedStoryArcEntry.imported_story_arc_id)
                .join(ImportedFile, ImportedFile.id == ImportedStoryArcEntry.import_file_id)
                .where(
                    ImportedStoryArcEntry.imported_story_arc_id.in_(arc_ids),
                    ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                )
                .distinct()
            )
        ).all()
    }


async def _load_entry_counts(
    session: AsyncSession,
    arc_ids: Sequence[int],
) -> dict[int, dict[StoryArcResolutionState, int]]:
    if not arc_ids:
        return {}
    result = await session.execute(
        select(
            ImportedStoryArcEntry.imported_story_arc_id,
            ImportedStoryArcEntry.resolution_state,
            func.count(ImportedStoryArcEntry.id),
        )
        .where(ImportedStoryArcEntry.imported_story_arc_id.in_(arc_ids))
        .group_by(
            ImportedStoryArcEntry.imported_story_arc_id,
            ImportedStoryArcEntry.resolution_state,
        )
    )
    counts: dict[int, dict[StoryArcResolutionState, int]] = {}
    for arc_id, state, count in result.all():
        counts.setdefault(int(arc_id), {})[state] = int(count)
    return counts


async def _load_merge_candidates(
    session: AsyncSession,
    arcs: Sequence[ImportedStoryArc],
) -> dict[int, tuple[StoryArcMergeCandidate, ...]]:
    if not arcs:
        return {}
    normalized_names = {arc.normalized_name for arc in arcs if arc.normalized_name}
    proposed_ids = {
        int(arc.proposed_story_arc_id) for arc in arcs if arc.proposed_story_arc_id is not None
    }
    if not normalized_names and not proposed_ids:
        return {}

    filters = []
    if normalized_names:
        filters.append(StoryArc.normalized_name.in_(normalized_names))
    if proposed_ids:
        filters.append(StoryArc.id.in_(proposed_ids))
    existing_arcs = list(
        (
            await session.execute(
                select(StoryArc)
                .where(
                    StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
                    or_(*filters),
                )
                .order_by(StoryArc.name.asc(), StoryArc.id.asc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )

    candidates: dict[int, tuple[StoryArcMergeCandidate, ...]] = {}
    for staged_arc in arcs:
        matches = [
            StoryArcMergeCandidate(id=int(existing.id), name=existing.name)
            for existing in existing_arcs
            if existing.normalized_name == staged_arc.normalized_name
            or existing.id == staged_arc.proposed_story_arc_id
        ]
        candidates[int(staged_arc.id)] = tuple(matches)
    return candidates


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
