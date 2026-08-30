"""Resolve staged story-arc entries from trusted local import evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.issue import Issue
from pullbox.models.story_arc import StoryArcResolutionState
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


CancellationCheck = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StoryArcResolutionResult:
    """Bounded reconciliation counts for review and diagnostics."""

    entries_examined: int = 0
    resolved: int = 0
    pending: int = 0
    missing: int = 0
    ambiguous: int = 0
    conflicts: int = 0
    skipped: int = 0
    linked_files: int = 0


@dataclass(frozen=True, slots=True)
class _ResolutionIndexes:
    files_by_id: dict[int, ImportedFile]
    files_by_path: dict[str, tuple[ImportedFile, ...]]
    files_by_cv_id: dict[int, tuple[ImportedFile, ...]]
    issues_by_id: dict[int, Issue]
    issues_by_cv_id: dict[int, Issue]


async def resolve_staged_story_arc_entries(
    session: AsyncSession,
    *,
    import_job_id: int,
    batch_size: int = 200,
    cancellation_check: CancellationCheck | None = None,
) -> StoryArcResolutionResult:
    """Reconcile staged entries without providers, source I/O, or canonical writes.

    Exact staged/import associations and trusted ComicVine issue identities are
    authoritative. Names, titles, order, and issue-number similarity are never
    used to manufacture a match. The caller owns the transaction.
    """
    if batch_size <= 0:
        msg = "Story-arc resolution batch size must be positive."
        raise ValueError(msg)

    await _checkpoint(cancellation_check)
    last_entry_id = 0
    counts = {state: 0 for state in StoryArcResolutionState}
    entries_examined = 0
    linked_files = 0

    while True:
        entries = list(
            (
                await session.scalars(
                    select(ImportedStoryArcEntry)
                    .join(
                        ImportedStoryArc,
                        ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
                    )
                    .where(
                        ImportedStoryArc.import_job_id == import_job_id,
                        ImportedStoryArcEntry.id > last_entry_id,
                    )
                    .order_by(ImportedStoryArcEntry.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not entries:
            break

        await _checkpoint(cancellation_check)
        indexes = await _load_resolution_indexes(
            session,
            import_job_id=import_job_id,
            entries=entries,
        )
        for entry in entries:
            previous_file_id = entry.import_file_id
            _resolve_entry(entry, indexes)
            if previous_file_id is None and entry.import_file_id is not None:
                linked_files += 1
            counts[entry.resolution_state] += 1
            entries_examined += 1

        await session.flush()
        last_entry_id = entries[-1].id

    return StoryArcResolutionResult(
        entries_examined=entries_examined,
        resolved=counts[StoryArcResolutionState.RESOLVED],
        pending=counts[StoryArcResolutionState.PENDING],
        missing=counts[StoryArcResolutionState.MISSING],
        ambiguous=counts[StoryArcResolutionState.AMBIGUOUS],
        conflicts=counts[StoryArcResolutionState.CONFLICT],
        skipped=counts[StoryArcResolutionState.SKIPPED],
        linked_files=linked_files,
    )


async def _load_resolution_indexes(
    session: AsyncSession,
    *,
    import_job_id: int,
    entries: Sequence[ImportedStoryArcEntry],
) -> _ResolutionIndexes:
    direct_file_ids = {
        entry.import_file_id for entry in entries if entry.import_file_id is not None
    }
    source_locations = {
        entry.source_location for entry in entries if entry.source_location is not None
    }
    source_cv_ids = {
        source_cv_id
        for entry in entries
        if (source_cv_id := _parse_provider_id(entry.source_issue_id)) is not None
    }

    file_filters = []
    if direct_file_ids:
        file_filters.append(ImportedFile.id.in_(direct_file_ids))
    if source_locations:
        file_filters.append(ImportedFile.file_path.in_(source_locations))
    if source_cv_ids:
        file_filters.extend(
            (
                ImportedFile.comicvine_issue_id.in_(source_cv_ids),
                ImportedFile.matched_issue_cv_id.in_(source_cv_ids),
            )
        )

    files: list[ImportedFile] = []
    if file_filters:
        files = list(
            (
                await session.scalars(
                    select(ImportedFile)
                    .where(
                        ImportedFile.import_job_id == import_job_id,
                        or_(*file_filters),
                    )
                    .order_by(ImportedFile.id)
                )
            ).all()
        )

    issue_ids = {entry.matched_issue_id for entry in entries if entry.matched_issue_id is not None}
    issue_ids.update(item.matched_issue_id for item in files if item.matched_issue_id is not None)
    issue_cv_ids = set(source_cv_ids)
    issue_cv_ids.update(
        item.matched_issue_cv_id for item in files if item.matched_issue_cv_id is not None
    )
    issue_cv_ids.update(
        item.comicvine_issue_id for item in files if item.comicvine_issue_id is not None
    )

    issue_filters = []
    if issue_ids:
        issue_filters.append(Issue.id.in_(issue_ids))
    if issue_cv_ids:
        issue_filters.append(Issue.comicvine_id.in_(issue_cv_ids))
    issues: list[Issue] = []
    if issue_filters:
        issues = list((await session.scalars(select(Issue).where(or_(*issue_filters)))).all())

    files_by_path_mutable: defaultdict[str, list[ImportedFile]] = defaultdict(list)
    files_by_cv_id_mutable: defaultdict[int, list[ImportedFile]] = defaultdict(list)
    for item in files:
        files_by_path_mutable[item.file_path].append(item)
        for provider_id in _trusted_file_provider_ids(item):
            files_by_cv_id_mutable[provider_id].append(item)

    return _ResolutionIndexes(
        files_by_id={item.id: item for item in files},
        files_by_path={key: tuple(value) for key, value in files_by_path_mutable.items()},
        files_by_cv_id={key: tuple(value) for key, value in files_by_cv_id_mutable.items()},
        issues_by_id={issue.id: issue for issue in issues},
        issues_by_cv_id={
            issue.comicvine_id: issue for issue in issues if issue.comicvine_id is not None
        },
    )


def _resolve_entry(entry: ImportedStoryArcEntry, indexes: _ResolutionIndexes) -> None:
    if entry.resolution_state == StoryArcResolutionState.SKIPPED:
        return

    source_cv_id = _parse_provider_id(entry.source_issue_id)
    existing_issue = (
        indexes.issues_by_id.get(entry.matched_issue_id)
        if entry.matched_issue_id is not None
        else None
    )
    if existing_issue is not None:
        if _issue_conflicts_with_source(existing_issue, source_cv_id):
            _mark_review(
                entry,
                StoryArcResolutionState.CONFLICT,
                "conflicting_exact_issue_identity",
            )
            return
        _mark_resolved(entry, existing_issue, method="existing_staged_issue")
        return

    candidate, candidate_conflict = _select_candidate_file(
        entry,
        source_cv_id=source_cv_id,
        indexes=indexes,
    )
    if candidate_conflict:
        _mark_review(
            entry,
            StoryArcResolutionState.CONFLICT,
            "conflicting_exact_issue_identity",
        )
        return

    if candidate is not None:
        entry.import_file_id = candidate.id
        if candidate.status == ImportedFileStatus.CONFLICT:
            _mark_review(
                entry,
                StoryArcResolutionState.CONFLICT,
                "source_file_identity_conflict",
            )
            return
        if candidate.status == ImportedFileStatus.SAFETY_BLOCKED:
            _mark_review(
                entry,
                StoryArcResolutionState.AMBIGUOUS,
                "source_file_safety_blocked",
            )
            return

        candidate_issue = (
            indexes.issues_by_id.get(candidate.matched_issue_id)
            if candidate.matched_issue_id is not None
            else None
        )
        if candidate_issue is not None:
            if _issue_conflicts_with_source(candidate_issue, source_cv_id):
                _mark_review(
                    entry,
                    StoryArcResolutionState.CONFLICT,
                    "conflicting_exact_issue_identity",
                )
                return
            method = (
                "linked_import_file"
                if entry.import_file_id == candidate.id and source_cv_id is None
                else "exact_source_issue_id"
            )
            _mark_resolved(entry, candidate_issue, method=method)
            return

        candidate_cv_id = candidate.matched_issue_cv_id or candidate.comicvine_issue_id
        if candidate_cv_id is not None:
            issue_by_file_identity = indexes.issues_by_cv_id.get(candidate_cv_id)
            if issue_by_file_identity is not None:
                _mark_resolved(entry, issue_by_file_identity, method="exact_import_file_identity")
                return

    if source_cv_id is not None:
        issue_by_source_identity = indexes.issues_by_cv_id.get(source_cv_id)
        if issue_by_source_identity is not None:
            _mark_resolved(entry, issue_by_source_identity, method="exact_source_issue_id")
            return

    if entry.resolution_state not in {
        StoryArcResolutionState.MISSING,
        StoryArcResolutionState.AMBIGUOUS,
        StoryArcResolutionState.CONFLICT,
    }:
        entry.resolution_state = StoryArcResolutionState.PENDING


def _select_candidate_file(
    entry: ImportedStoryArcEntry,
    *,
    source_cv_id: int | None,
    indexes: _ResolutionIndexes,
) -> tuple[ImportedFile | None, bool]:
    if entry.import_file_id is not None:
        direct = indexes.files_by_id.get(entry.import_file_id)
        if direct is not None:
            return direct, _file_conflicts_with_source(direct, source_cv_id)

    if entry.source_location is not None:
        by_path = indexes.files_by_path.get(entry.source_location, ())
        selected, conflict = _select_convergent_file(by_path)
        if conflict:
            return None, True
        if selected is not None:
            return selected, _file_conflicts_with_source(selected, source_cv_id)

    if source_cv_id is not None:
        return _select_convergent_file(indexes.files_by_cv_id.get(source_cv_id, ()))
    return None, False


def _select_convergent_file(
    candidates: Sequence[ImportedFile],
) -> tuple[ImportedFile | None, bool]:
    if not candidates:
        return None, False
    matched_issue_ids = {
        candidate.matched_issue_id
        for candidate in candidates
        if candidate.matched_issue_id is not None
    }
    matched_cv_ids = {
        candidate.matched_issue_cv_id
        for candidate in candidates
        if candidate.matched_issue_cv_id is not None
    }
    if len(matched_issue_ids) > 1 or len(matched_cv_ids) > 1:
        return None, True
    return min(candidates, key=lambda item: item.id), False


def _file_conflicts_with_source(item: ImportedFile, source_cv_id: int | None) -> bool:
    trusted_ids = _trusted_file_provider_ids(item)
    return source_cv_id is not None and bool(trusted_ids) and source_cv_id not in trusted_ids


def _issue_conflicts_with_source(issue: Issue, source_cv_id: int | None) -> bool:
    return (
        source_cv_id is not None
        and issue.comicvine_id is not None
        and issue.comicvine_id != source_cv_id
    )


def _trusted_file_provider_ids(item: ImportedFile) -> set[int]:
    return {
        value for value in (item.comicvine_issue_id, item.matched_issue_cv_id) if value is not None
    }


def _mark_resolved(entry: ImportedStoryArcEntry, issue: Issue, *, method: str) -> None:
    entry.matched_issue_id = issue.id
    entry.resolution_state = StoryArcResolutionState.RESOLVED
    entry.resolution_confidence = 1.0
    entry.resolution_method = method
    diagnostics = dict(entry.diagnostics or {})
    diagnostics.pop("review_reason", None)
    diagnostics["resolution_evidence"] = "trusted_local_exact_identity"
    entry.diagnostics = diagnostics


def _mark_review(
    entry: ImportedStoryArcEntry,
    state: StoryArcResolutionState,
    reason: str,
) -> None:
    entry.matched_issue_id = None
    entry.resolution_state = state
    entry.resolution_confidence = None
    entry.resolution_method = None
    diagnostics = dict(entry.diagnostics or {})
    diagnostics["review_reason"] = reason
    entry.diagnostics = diagnostics


def _parse_provider_id(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized.isdecimal():
        return None
    parsed = int(normalized)
    return parsed if 0 < parsed <= 2**63 - 1 else None


async def _checkpoint(callback: CancellationCheck | None) -> None:
    if callback is not None:
        await callback()
