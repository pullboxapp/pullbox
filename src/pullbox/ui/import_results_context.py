"""Context loading for import Step 5 results."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.series import IssueCatalogState, Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    count_completed_import_cleanup_scope,
    list_completed_import_cleanup_examples,
)
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    import_safety_category_label,
)
from pullbox.services.import_workflow_state import import_control_state_for_job

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_STORY_ARC_ACTION_PAGE_SIZE = 1_000
_FAILED_SAFETY_DETAIL_LIMIT = 100
_STORY_ARC_MANAGED_ACTION = "story_arc_managed_placement_requested"
_STORY_ARC_REFERENCE_ACTION = "story_arc_referenced_placement_attached"
_MANAGED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "sync_work_id",
        "membership_id",
        "desired_generation",
        "imported_story_arc_id",
        "imported_story_arc_entry_id",
        "source_import_job_id",
    }
)
_REFERENCE_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "journal_state",
        "placement_id",
        "issue_story_arc_id",
        "imported_story_arc_entry_id",
        "placement_path",
        "source_kind",
        "source_import_job_id",
        "expected_after",
    }
)
_MANAGED_PLACEMENT_MODES = frozenset(
    {
        StoryArcPlacementMode.COPY,
        StoryArcPlacementMode.HARDLINK,
        StoryArcPlacementMode.SYMLINK,
    }
)


async def _count_series_status(
    session: AsyncSession,
    job_id: int,
    status: ImportSeriesStatus,
) -> int:
    return int(
        (
            await session.execute(
                select(func.count(ImportedSeries.id)).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == status,
                )
            )
        ).scalar_one()
        or 0
    )


async def _load_file_status_counts(session: AsyncSession, job_id: int) -> dict[str, int]:
    file_status_counts: dict[str, int] = {}
    for file_status in ImportedFileStatus:
        count = int(
            (
                await session.execute(
                    select(func.count(ImportedFile.id)).where(
                        ImportedFile.import_job_id == job_id,
                        ImportedFile.status == file_status,
                    )
                )
            ).scalar_one()
            or 0
        )
        if count > 0:
            file_status_counts[file_status.value] = count
    return file_status_counts


async def _load_files_for_status(
    session: AsyncSession,
    job_id: int,
    status: ImportedFileStatus,
    *,
    limit: int | None = None,
) -> list[ImportedFile]:
    query = (
        select(ImportedFile)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == status,
        )
        .order_by(ImportedFile.id)
    )
    if limit is not None:
        query = query.limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())


_SAFETY_ACTION_BY_CATEGORY = {
    ImportSafetyCategory.SOURCE_MISSING: (
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        "safe_action",
    ),
    ImportSafetyCategory.SINGLE_PAGE_COMIC: (
        CompletedImportCleanupAction.SKIP_PROBABLE_COVERS,
        "safe_action",
    ),
    ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT: (
        CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES,
        "safe_action",
    ),
    ImportSafetyCategory.PERMISSION_UNREADABLE: (
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        "safe_action",
    ),
    ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED: (
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        "safe_action",
    ),
    ImportSafetyCategory.SOURCE_CHANGED: (
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        "safe_action",
    ),
    ImportSafetyCategory.ZERO_BYTE: (
        CompletedImportCleanupAction.SKIP_UNUSABLE_FILES,
        "safe_action",
    ),
    ImportSafetyCategory.ARCHIVE_NO_PAGES: (
        CompletedImportCleanupAction.SKIP_UNUSABLE_FILES,
        "safe_action",
    ),
    ImportSafetyCategory.UNSUPPORTED_FILE_TYPE: (
        CompletedImportCleanupAction.SKIP_UNUSABLE_FILES,
        "safe_action",
    ),
}

_CLEANUP_ACTION_PRESENTATION = {
    CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES: {
        "label": "Dismiss stale Mylar references",
        "description": (
            "Clear database references whose source files no longer exist. No files are deleted."
        ),
        "button_label": "Dismiss references",
        "tone": "neutral",
    },
    CompletedImportCleanupAction.SKIP_PROBABLE_COVERS: {
        "label": "Skip probable cover files",
        "description": (
            "Exclude one-page image archives that look like series cover art, "
            "while preserving the source files."
        ),
        "button_label": "Skip cover files",
        "tone": "neutral",
    },
    CompletedImportCleanupAction.SKIP_UNUSABLE_FILES: {
        "label": "Skip unusable files",
        "description": (
            "Clear empty, unsupported, or page-less files that cannot become library issues."
        ),
        "button_label": "Skip unusable files",
        "tone": "neutral",
    },
    CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES: {
        "label": "Allow oversized files once",
        "description": (
            "Retry legitimate large books once without weakening the global archive safety policy."
        ),
        "button_label": "Allow once and retry",
        "tone": "warning",
    },
    CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION: {
        "label": "Retry source inspection",
        "description": (
            "Recheck files that were unreadable, changed, or could not be "
            "inspected during the original run."
        ),
        "button_label": "Recheck sources",
        "tone": "warning",
    },
    CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED: {
        "label": "Recognize already-owned issues",
        "description": (
            "Resolve conflicts that point to issues already registered in the Pullbox library."
        ),
        "button_label": "Mark already owned",
        "tone": "neutral",
    },
    CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS: {
        "label": "Accept recommended conflict choices",
        "description": (
            "Import the single high-confidence preferred file in each eligible "
            "conflict group and skip its alternatives."
        ),
        "button_label": "Accept recommendations",
        "tone": "warning",
    },
}


async def _load_cleanup_action_summaries(
    session: AsyncSession,
    job_id: int,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for action, presentation in _CLEANUP_ACTION_PRESENTATION.items():
        affected_count, affected_file_count = await count_completed_import_cleanup_scope(
            session,
            job_id,
            action,
        )
        if affected_count == 0:
            continue
        summaries.append(
            {
                "action": action.value,
                "affected_count": affected_count,
                "affected_file_count": affected_file_count,
                "item_unit": (
                    "group"
                    if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS
                    else "file"
                ),
                "examples": await list_completed_import_cleanup_examples(
                    session,
                    job_id,
                    action,
                ),
                **presentation,
            }
        )
    return summaries


async def _load_safety_category_summaries(
    session: AsyncSession,
    job_id: int,
) -> list[dict[str, object]]:
    category_expression = ImportedFile.diagnostics["safety_block"]["category"].as_string()
    rows = (
        await session.execute(
            select(category_expression, func.count(ImportedFile.id))
            .where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
            )
            .group_by(category_expression)
            .order_by(func.count(ImportedFile.id).desc(), category_expression.asc())
        )
    ).all()
    summaries: list[dict[str, object]] = []
    for raw_category, count in rows:
        try:
            category = ImportSafetyCategory(str(raw_category))
        except ValueError:
            category = ImportSafetyCategory.UNKNOWN
        action, bucket = _SAFETY_ACTION_BY_CATEGORY.get(category, (None, "needs_review"))
        examples = tuple(
            (
                await session.scalars(
                    select(ImportedFile.file_name)
                    .where(
                        ImportedFile.import_job_id == job_id,
                        ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                        category_expression == raw_category,
                    )
                    .order_by(ImportedFile.id)
                    .limit(3)
                )
            ).all()
        )
        summaries.append(
            {
                "category": category.value,
                "label": import_safety_category_label(category),
                "count": int(count),
                "examples": examples,
                "action": action.value if action is not None else None,
                "bucket": bucket,
            }
        )
    return summaries


async def _orphaned_file_no_match_count(session: AsyncSession, job_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count(ImportedFile.id))
                .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
                .where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.status == ImportedFileStatus.NO_MATCH,
                    ImportedSeries.status.in_(
                        [
                            ImportSeriesStatus.NO_MATCH,
                            ImportSeriesStatus.RECOVERY_PENDING,
                        ]
                    ),
                )
            )
        ).scalar_one()
        or 0
    )


async def _load_catalog_sync_series(session: AsyncSession, job_id: int) -> list[Series]:
    """Return imported series whose full ComicVine issue catalog is not complete yet."""
    state_rank = {
        IssueCatalogState.FAILED: 0,
        IssueCatalogState.PARTIAL: 1,
        IssueCatalogState.HYDRATING: 2,
    }
    result = await session.execute(
        select(Series)
        .join(ImportedSeries, ImportedSeries.series_id == Series.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.IMPORTED,
            Series.issue_catalog_state.in_(
                [
                    IssueCatalogState.HYDRATING,
                    IssueCatalogState.PARTIAL,
                    IssueCatalogState.FAILED,
                ]
            ),
        )
        .order_by(
            case(
                *[
                    (Series.issue_catalog_state == state, rank)
                    for state, rank in state_rank.items()
                ],
                else_=99,
            ),
            Series.sort_title.asc(),
        )
    )
    return list(result.unique().scalars().all())


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _managed_story_arc_action_is_completed(
    *,
    job_id: int,
    action: ImportJobAction,
    work: StoryArcSyncWork | None,
    placements: list[StoryArcPlacement],
    staged_arc: ImportedStoryArc | None,
    staged_entry: ImportedStoryArcEntry | None,
    membership: IssueStoryArc | None,
    library_file: LibraryFile | None,
) -> bool:
    payload = dict(action.payload or {})
    if (
        action.phase != "story_arc_placements"
        or set(payload) != _MANAGED_PAYLOAD_KEYS
        or _positive_int(payload.get("schema_version")) != 1
        or work is None
        or work.state is not StoryArcSyncWorkState.COMPLETED
        or work.origin_import_action_id != action.id
        or work.origin_import_job_id != job_id
        or _positive_int(payload.get("sync_work_id")) != work.id
        or _positive_int(payload.get("membership_id")) != work.issue_story_arc_id
        or payload.get("desired_generation") != work.desired_generation
        or _positive_int(payload.get("imported_story_arc_id")) != work.origin_imported_story_arc_id
        or _positive_int(payload.get("imported_story_arc_entry_id"))
        != work.origin_imported_story_arc_entry_id
        or _positive_int(payload.get("source_import_job_id")) != job_id
        or staged_arc is None
        or staged_arc.import_job_id != job_id
        or staged_arc.status is not ImportedStoryArcStatus.IMPORTED
        or staged_entry is None
        or staged_entry.imported_story_arc_id != staged_arc.id
        or staged_entry.materialized_membership_id != work.issue_story_arc_id
        or staged_entry.resolution_state is not StoryArcResolutionState.RESOLVED
        or membership is None
        or staged_arc.materialized_story_arc_id != membership.story_arc_id
        or library_file is None
        or staged_entry.matched_issue_id != library_file.issue_id
        or membership.issue_id != library_file.issue_id
        or len(placements) != 1
    ):
        return False
    placement = placements[0]
    return bool(
        placement.issue_story_arc_id == work.issue_story_arc_id
        and placement.library_file_id == work.library_file_id
        and placement.source_import_job_id == job_id
        and placement.creating_action_id == action.id
        and placement.ownership is StoryArcPlacementOwnership.MANAGED
        and placement.mode in _MANAGED_PLACEMENT_MODES
        and placement.state is StoryArcPlacementState.CURRENT
        and placement.source_kind is StoryArcSourceKind.PULLBOX
        and placement.policy_schema_version == work.policy_schema_version
        and placement.rendered_reading_order == work.membership_sequence
        and placement.operation_token is None
        and dict(placement.last_result or {}).get("status") == "complete"
    )


def _referenced_story_arc_action_is_completed(
    *,
    job_id: int,
    action: ImportJobAction,
    placements: list[StoryArcPlacement],
    staged_arc: ImportedStoryArc | None,
    staged_entry: ImportedStoryArcEntry | None,
) -> bool:
    payload = dict(action.payload or {})
    placement_id = _positive_int(payload.get("placement_id"))
    membership_id = _positive_int(payload.get("issue_story_arc_id"))
    if (
        action.phase != "story_arcs"
        or set(payload) != _REFERENCE_PAYLOAD_KEYS
        or _positive_int(payload.get("schema_version")) != 1
        or payload.get("journal_state") != "completed"
        or placement_id is None
        or membership_id is None
        or _positive_int(payload.get("source_import_job_id")) != job_id
        or not isinstance(payload.get("expected_after"), dict)
        or staged_arc is None
        or staged_arc.import_job_id != job_id
        or staged_arc.status is not ImportedStoryArcStatus.IMPORTED
        or staged_entry is None
        or staged_entry.imported_story_arc_id != staged_arc.id
        or staged_entry.materialized_membership_id != membership_id
        or staged_entry.resolution_state is not StoryArcResolutionState.RESOLVED
        or staged_entry.source_kind.value != payload.get("source_kind")
        or len(placements) != 1
    ):
        return False
    placement = placements[0]
    return bool(
        placement.id == placement_id
        and placement.issue_story_arc_id == membership_id
        and placement.placement_path == payload.get("placement_path")
        and placement.mode is StoryArcPlacementMode.REFERENCE_ONLY
        and placement.ownership is StoryArcPlacementOwnership.REFERENCED
        and placement.source_kind.value == payload.get("source_kind")
        and placement.source_import_job_id == job_id
        and placement.creating_action_id == action.id
    )


async def _load_story_arc_ownership_counts(
    session: AsyncSession,
    job_id: int,
) -> dict[str, int]:
    """Count only durable, completed, import-owned Story Arc placements."""
    counts = {"managed": 0, "referenced": 0}
    after_action_id = 0
    while True:
        actions = list(
            (
                await session.scalars(
                    select(ImportJobAction)
                    .where(
                        ImportJobAction.import_job_id == job_id,
                        ImportJobAction.status == ImportJobActionStatus.COMPLETED,
                        ImportJobAction.action_type.in_(
                            [_STORY_ARC_MANAGED_ACTION, _STORY_ARC_REFERENCE_ACTION]
                        ),
                        ImportJobAction.id > after_action_id,
                    )
                    .order_by(ImportJobAction.id.asc())
                    .limit(_STORY_ARC_ACTION_PAGE_SIZE)
                )
            ).all()
        )
        if not actions:
            break
        action_ids = [int(action.id) for action in actions]
        works = list(
            (
                await session.scalars(
                    select(StoryArcSyncWork).where(
                        StoryArcSyncWork.origin_import_action_id.in_(action_ids)
                    )
                )
            ).all()
        )
        work_by_action_id = {
            int(work.origin_import_action_id): work
            for work in works
            if work.origin_import_action_id is not None
        }
        placements_by_action_id: dict[int, list[StoryArcPlacement]] = {}
        for placement in (
            await session.scalars(
                select(StoryArcPlacement).where(
                    StoryArcPlacement.creating_action_id.in_(action_ids)
                )
            )
        ).all():
            if placement.creating_action_id is not None:
                placements_by_action_id.setdefault(int(placement.creating_action_id), []).append(
                    placement
                )

        imported_arc_ids = {
            int(work.origin_imported_story_arc_id)
            for work in works
            if work.origin_imported_story_arc_id is not None
        }
        imported_entry_ids = {
            int(work.origin_imported_story_arc_entry_id)
            for work in works
            if work.origin_imported_story_arc_entry_id is not None
        }
        for action in actions:
            payload = dict(action.payload or {})
            arc_id = _positive_int(payload.get("imported_story_arc_id"))
            entry_id = _positive_int(payload.get("imported_story_arc_entry_id"))
            if arc_id is not None:
                imported_arc_ids.add(arc_id)
            if entry_id is not None:
                imported_entry_ids.add(entry_id)
        entries_by_id = {
            int(entry.id): entry
            for entry in (
                await session.scalars(
                    select(ImportedStoryArcEntry).where(
                        ImportedStoryArcEntry.id.in_(imported_entry_ids)
                    )
                )
            ).all()
        }
        imported_arc_ids.update(
            int(entry.imported_story_arc_id) for entry in entries_by_id.values()
        )
        arcs_by_id = {
            int(arc.id): arc
            for arc in (
                await session.scalars(
                    select(ImportedStoryArc).where(ImportedStoryArc.id.in_(imported_arc_ids))
                )
            ).all()
        }
        membership_ids = {int(work.issue_story_arc_id) for work in works}
        library_file_ids = {int(work.library_file_id) for work in works}
        memberships_by_id = {
            int(membership.id): membership
            for membership in (
                await session.scalars(
                    select(IssueStoryArc).where(IssueStoryArc.id.in_(membership_ids))
                )
            ).all()
        }
        library_files_by_id = {
            int(library_file.id): library_file
            for library_file in (
                await session.scalars(
                    select(LibraryFile).where(LibraryFile.id.in_(library_file_ids))
                )
            ).all()
        }
        for action in actions:
            placements = placements_by_action_id.get(int(action.id), [])
            if action.action_type == _STORY_ARC_REFERENCE_ACTION:
                entry_id = _positive_int(
                    dict(action.payload or {}).get("imported_story_arc_entry_id")
                )
                entry = entries_by_id.get(entry_id) if entry_id is not None else None
                arc = (
                    arcs_by_id.get(int(entry.imported_story_arc_id)) if entry is not None else None
                )
                if _referenced_story_arc_action_is_completed(
                    job_id=job_id,
                    action=action,
                    placements=placements,
                    staged_arc=arc,
                    staged_entry=entry,
                ):
                    counts["referenced"] += 1
                continue
            work = work_by_action_id.get(int(action.id))
            if work is None:
                continue
            if _managed_story_arc_action_is_completed(
                job_id=job_id,
                action=action,
                work=work,
                placements=placements,
                staged_arc=arcs_by_id.get(int(work.origin_imported_story_arc_id or 0)),
                staged_entry=entries_by_id.get(int(work.origin_imported_story_arc_entry_id or 0)),
                membership=memberships_by_id.get(int(work.issue_story_arc_id)),
                library_file=library_files_by_id.get(int(work.library_file_id)),
            ):
                counts["managed"] += 1
        after_action_id = int(actions[-1].id)
    return counts


async def _load_rollback_journal_summary(
    session: AsyncSession,
    job_id: int,
) -> dict[str, int]:
    """Summarize file ownership without materializing a large action journal."""
    storage_mode = ImportJobAction.payload["storage_mode"].as_string()
    transfer_method = ImportJobAction.payload["transfer_method"].as_string()
    ownership = case(
        (
            (storage_mode == "referenced") | (transfer_method == "leave_in_place"),
            "referenced",
        ),
        else_="managed",
    ).label("ownership")
    result = await session.execute(
        select(
            ownership,
            ImportJobAction.status,
            func.count(ImportJobAction.id),
        )
        .where(
            ImportJobAction.import_job_id == job_id,
            ImportJobAction.action_type == "library_file_registered",
        )
        .group_by(ownership, ImportJobAction.status)
    )

    completed = {"managed": 0, "referenced": 0}
    for owner, status, count in result.all():
        if status == ImportJobActionStatus.COMPLETED:
            completed[str(owner)] = int(count or 0)
    story_arc_completed = await _load_story_arc_ownership_counts(session, job_id)
    completed["managed"] += story_arc_completed["managed"]
    completed["referenced"] += story_arc_completed["referenced"]

    action_status_result = await session.execute(
        select(ImportJobAction.status, func.count(ImportJobAction.id))
        .where(ImportJobAction.import_job_id == job_id)
        .group_by(ImportJobAction.status)
    )
    action_status_counts = {status: int(count or 0) for status, count in action_status_result.all()}
    completed_action_count = action_status_counts.get(ImportJobActionStatus.COMPLETED, 0)
    rolled_back_action_count = action_status_counts.get(ImportJobActionStatus.ROLLED_BACK, 0)
    manual_recovery_count = action_status_counts.get(
        ImportJobActionStatus.ROLLBACK_FAILED,
        0,
    )
    return {
        "managed_artifacts_created": completed["managed"],
        "referenced_files_registered": completed["referenced"],
        # These are journal candidates, not an assertion that the on-disk artifact
        # is still unchanged. Rollback revalidates ownership and fingerprints.
        "rollback_managed_candidates": completed["managed"],
        "rollback_reference_candidates": completed["referenced"],
        "rollback_manual_recovery_count": manual_recovery_count,
        "rollback_action_count": sum(action_status_counts.values()),
        "rollback_actions_pending": completed_action_count,
        "rollback_actions_rolled_back": rolled_back_action_count,
    }


async def load_import_results_context(
    session: AsyncSession,
    job: ImportJob,
) -> dict[str, object]:
    """Load aggregate counts and detail rows for the Step 5 results template."""
    job_id = int(job.id)
    imported_count = await _count_series_status(session, job_id, ImportSeriesStatus.IMPORTED)
    failed_count = await _count_series_status(session, job_id, ImportSeriesStatus.FAILED)
    duplicate_count = await _count_series_status(session, job_id, ImportSeriesStatus.DUPLICATE)
    no_match_count = await _count_series_status(session, job_id, ImportSeriesStatus.NO_MATCH)
    recovery_pending_count = await _count_series_status(
        session,
        job_id,
        ImportSeriesStatus.RECOVERY_PENDING,
    )
    imported_issue_recovery_count = int(
        (
            await session.execute(
                select(func.count(ImportedSeries.id)).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == ImportSeriesStatus.IMPORTED,
                    select(ImportedFile.id)
                    .where(
                        ImportedFile.import_series_id == ImportedSeries.id,
                        ImportedFile.status == ImportedFileStatus.NO_MATCH,
                    )
                    .exists(),
                )
            )
        ).scalar_one()
        or 0
    )
    unmatched_queue_count = no_match_count + recovery_pending_count + imported_issue_recovery_count

    failed_series: list[ImportedSeries] = []
    if failed_count > 0:
        result = await session.execute(
            select(ImportedSeries).where(
                ImportedSeries.import_job_id == job_id,
                ImportedSeries.status == ImportSeriesStatus.FAILED,
            )
        )
        failed_series = list(result.scalars().all())

    file_status_counts = await _load_file_status_counts(session, job_id)
    files_imported = max(
        file_status_counts.get(ImportedFileStatus.IMPORTED.value, 0),
        job.total_files_imported or 0,
    )
    files_matched = max(
        file_status_counts.get(ImportedFileStatus.MATCHED.value, 0),
        job.total_files_matched or 0,
    )
    files_duplicate = max(
        file_status_counts.get(ImportedFileStatus.DUPLICATE_FILE.value, 0),
        job.total_files_duplicate or 0,
    )
    files_already_owned = max(
        file_status_counts.get(ImportedFileStatus.ALREADY_OWNED.value, 0),
        job.total_files_already_owned or 0,
    )
    files_conflict = max(
        file_status_counts.get(ImportedFileStatus.CONFLICT.value, 0),
        job.total_files_conflict or 0,
    )
    files_no_match = max(
        file_status_counts.get(ImportedFileStatus.NO_MATCH.value, 0),
        job.total_files_no_match or 0,
    )
    files_failed = max(
        file_status_counts.get(ImportedFileStatus.FAILED.value, 0),
        job.total_files_failed or 0,
    )
    files_skipped = file_status_counts.get(ImportedFileStatus.SKIPPED.value, 0)
    failed_files = (
        await _load_files_for_status(session, job_id, ImportedFileStatus.FAILED)
        if files_failed > 0
        else []
    )
    source_changed_files = sum(
        1
        for imported_file in failed_files
        if dict(dict(imported_file.diagnostics or {}).get("source_revalidation") or {}).get("code")
        == "source_changed"
    )
    files_safety_blocked = file_status_counts.get(
        ImportedFileStatus.SAFETY_BLOCKED.value,
        0,
    )
    safety_blocked_files = (
        await _load_files_for_status(
            session,
            job_id,
            ImportedFileStatus.SAFETY_BLOCKED,
            limit=_FAILED_SAFETY_DETAIL_LIMIT,
        )
        if files_safety_blocked > 0 and job.status is ImportJobStatus.FAILED
        else []
    )
    safety_category_summaries = (
        await _load_safety_category_summaries(session, job_id) if files_safety_blocked > 0 else []
    )
    cleanup_action_summaries = (
        await _load_cleanup_action_summaries(session, job_id)
        if job.status is ImportJobStatus.COMPLETED and job.archived_at is None
        else []
    )
    cleanup_by_action = {str(item["action"]): item for item in cleanup_action_summaries}
    recommended_summary = cleanup_by_action.get(
        CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS.value,
        {},
    )
    already_owned_summary = cleanup_by_action.get(
        CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED.value,
        {},
    )
    recommended_conflict_groups = _positive_int(recommended_summary.get("affected_count")) or 0
    recommended_conflict_files = _positive_int(recommended_summary.get("affected_file_count")) or 0
    already_owned_conflict_files = (
        _positive_int(already_owned_summary.get("affected_file_count")) or 0
    )
    remaining_conflict_files = max(
        files_conflict - recommended_conflict_files - already_owned_conflict_files,
        0,
    )
    actionable_safety_files = sum(
        _positive_int(item["affected_file_count"]) or 0
        for item in cleanup_action_summaries
        if item["action"]
        in {
            CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES.value,
            CompletedImportCleanupAction.SKIP_PROBABLE_COVERS.value,
            CompletedImportCleanupAction.SKIP_UNUSABLE_FILES.value,
            CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES.value,
            CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION.value,
        }
    )
    needs_review_safety_count = max(files_safety_blocked - actionable_safety_files, 0)
    files_total = sum(file_status_counts.values())
    orphaned_file_no_match_count = await _orphaned_file_no_match_count(session, job_id)
    identified_series_file_no_match_count = max(
        files_no_match - orphaned_file_no_match_count,
        0,
    )
    catalog_sync_series = await _load_catalog_sync_series(session, job_id)
    catalog_sync_failed_count = sum(
        1
        for series in catalog_sync_series
        if series.issue_catalog_state == IssueCatalogState.FAILED
    )
    catalog_sync_pending_count = len(catalog_sync_series) - catalog_sync_failed_count
    rollback_journal_summary = await _load_rollback_journal_summary(session, job_id)
    rollback_incomplete = bool(
        rollback_journal_summary["rollback_manual_recovery_count"]
        and job.status == ImportJobStatus.FAILED
        and dict(job.progress_snapshot or {}).get("mode") == "rollback"
    )
    can_rollback = bool(import_control_state_for_job(job).get("can_rollback")) and not (
        rollback_incomplete
    )

    return {
        "can_rollback": can_rollback,
        "rollback_incomplete": rollback_incomplete,
        "imported_count": imported_count,
        "failed_count": failed_count,
        "duplicate_count": duplicate_count,
        "no_match_count": no_match_count,
        "unmatched_queue_count": unmatched_queue_count,
        "failed_series": failed_series,
        "files_total": files_total,
        "files_imported": files_imported,
        "files_matched": files_matched,
        "files_duplicate": files_duplicate,
        "files_already_owned": files_already_owned,
        "files_conflict": files_conflict,
        "files_no_match": files_no_match,
        "orphaned_file_no_match_count": orphaned_file_no_match_count,
        "identified_series_file_no_match_count": identified_series_file_no_match_count,
        "catalog_sync_pending_count": catalog_sync_pending_count,
        "catalog_sync_failed_count": catalog_sync_failed_count,
        "catalog_sync_attention_count": len(catalog_sync_series),
        "catalog_sync_series": catalog_sync_series,
        "files_failed": files_failed,
        "files_skipped": files_skipped,
        "source_changed_files": source_changed_files,
        "failed_files": failed_files,
        "files_safety_blocked": files_safety_blocked,
        # Completed results use category summaries. Failed jobs retain a
        # bounded detail list so interrupted safety decisions remain actionable.
        "safety_blocked_files": safety_blocked_files,
        "safety_blocked_files_truncated": max(
            files_safety_blocked - len(safety_blocked_files),
            0,
        ),
        "safety_category_summaries": safety_category_summaries,
        "cleanup_action_summaries": cleanup_action_summaries,
        "recommended_conflict_groups": recommended_conflict_groups,
        "recommended_conflict_files": recommended_conflict_files,
        "already_owned_conflict_files": already_owned_conflict_files,
        "remaining_conflict_files": remaining_conflict_files,
        "cleanup_no_action_count": files_duplicate + files_already_owned + files_skipped,
        "cleanup_safe_action_count": sum(
            _positive_int(item["affected_file_count"]) or 0 for item in cleanup_action_summaries
        ),
        "cleanup_needs_review_count": needs_review_safety_count + remaining_conflict_files,
        **rollback_journal_summary,
    }
