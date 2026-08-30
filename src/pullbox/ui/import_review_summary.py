"""Import review summary count loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.story_arc import ImportedStoryArcStatus, StoryArcResolutionState
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_review_selection import load_import_review_selection_state
from pullbox.services.import_safety_diagnostics import summarize_import_safety_failures

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob


def _object_to_int(value: object, default: int = 0) -> int:
    try:
        return int(cast("Any", value))
    except (TypeError, ValueError):
        return default


async def load_import_review_summary(
    session: AsyncSession,
    job: ImportJob,
) -> dict[str, int]:
    """Return canonical review counts shared by wizard step 2 and step 3.

    During active scan phases we blend live job counters with persisted row counts so
    the cards can update before all rows exist. Once the job reaches review or later,
    the imported series/file rows become the authoritative source because user actions
    in step 3 can change those statuses.
    """
    series_counts_result = await session.execute(
        select(ImportedSeries.status, func.count(ImportedSeries.id))
        .where(ImportedSeries.import_job_id == job.id)
        .group_by(ImportedSeries.status)
    )
    series_counts = {
        status.value if hasattr(status, "value") else str(status): count
        for status, count in series_counts_result.all()
    }

    file_counts_result = await session.execute(
        select(ImportedFile.status, func.count(ImportedFile.id))
        .where(ImportedFile.import_job_id == job.id)
        .group_by(ImportedFile.status)
    )
    file_counts = {
        status.value if hasattr(status, "value") else str(status): count
        for status, count in file_counts_result.all()
    }

    duplicate_file_counts_result = await session.execute(
        select(ImportedFile.status, func.count(ImportedFile.id))
        .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
        )
        .group_by(ImportedFile.status)
    )
    duplicate_file_counts = {
        status.value if hasattr(status, "value") else str(status): count
        for status, count in duplicate_file_counts_result.all()
    }
    series_candidate_conflicts_result = await session.execute(
        select(ImportedSeries.diagnostics).where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
        )
    )
    series_candidate_conflicts = sum(
        1
        for (diagnostics,) in series_candidate_conflicts_result.all()
        if dict(diagnostics or {}).get("kind") == "series_conflict"
    )
    selection_state = await load_import_review_selection_state(session, job.id)
    duplicate_selected_count = _object_to_int(selection_state["duplicate_files_selected"])
    duplicate_selected_series_count = _object_to_int(selection_state["duplicate_series_selected"])
    matched_selected_series_count = _object_to_int(selection_state["matched_series_selected"])
    duplicate_importable_series_count = _object_to_int(
        selection_state["duplicate_series_importable"]
    )
    resolved_file_conflict_groups = int(
        (
            await session.execute(
                select(func.count(func.distinct(ImportedFile.conflict_group_id))).where(
                    ImportedFile.import_job_id == job.id,
                    ImportedFile.conflict_group_id.is_not(None),
                    ImportedFile.status.in_(
                        [ImportedFileStatus.CONFIRMED, ImportedFileStatus.SKIPPED]
                    ),
                )
            )
        ).scalar_one()
    )
    story_arc_counts_result = await session.execute(
        select(ImportedStoryArc.status, func.count(ImportedStoryArc.id))
        .where(ImportedStoryArc.import_job_id == job.id)
        .group_by(ImportedStoryArc.status)
    )
    story_arc_counts = {
        status.value if hasattr(status, "value") else str(status): int(count)
        for status, count in story_arc_counts_result.all()
    }
    story_arc_entry_counts_result = await session.execute(
        select(ImportedStoryArcEntry.resolution_state, func.count(ImportedStoryArcEntry.id))
        .join(
            ImportedStoryArc,
            ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
        )
        .where(ImportedStoryArc.import_job_id == job.id)
        .group_by(ImportedStoryArcEntry.resolution_state)
    )
    story_arc_entry_counts = {
        state.value if hasattr(state, "value") else str(state): int(count)
        for state, count in story_arc_entry_counts_result.all()
    }
    story_arcs_selected = int(
        await session.scalar(
            select(func.count(ImportedStoryArc.id)).where(
                ImportedStoryArc.import_job_id == job.id,
                ImportedStoryArc.selected_for_import.is_(True),
            )
        )
        or 0
    )
    story_arc_reviewable_statuses = {
        ImportedStoryArcStatus.DETECTED.value,
        ImportedStoryArcStatus.NEEDS_REVIEW.value,
        ImportedStoryArcStatus.READY.value,
        ImportedStoryArcStatus.CONFIRMED.value,
    }
    story_arcs_reviewable = sum(
        count
        for status, count in story_arc_counts.items()
        if status in story_arc_reviewable_statuses
    )

    row_summary = {
        "series_total": sum(series_counts.values()),
        "series_in_library": series_counts.get(ImportSeriesStatus.DUPLICATE.value, 0),
        "series_matched": series_counts.get(ImportSeriesStatus.MATCHED.value, 0),
        "series_confirmed": series_counts.get(ImportSeriesStatus.CONFIRMED.value, 0),
        "series_no_match": max(
            series_counts.get(ImportSeriesStatus.NO_MATCH.value, 0) - series_candidate_conflicts,
            0,
        ),
        "series_candidate_conflicts": series_candidate_conflicts,
        "series_imported": series_counts.get(ImportSeriesStatus.IMPORTED.value, 0),
        "series_failed": series_counts.get(ImportSeriesStatus.FAILED.value, 0),
        "files_total": sum(file_counts.values()),
        "files_matched": file_counts.get(ImportedFileStatus.MATCHED.value, 0),
        "files_duplicate": file_counts.get(ImportedFileStatus.DUPLICATE_FILE.value, 0),
        "files_already_owned": file_counts.get(ImportedFileStatus.ALREADY_OWNED.value, 0),
        "files_confirmed": file_counts.get(ImportedFileStatus.CONFIRMED.value, 0),
        "files_conflict": file_counts.get(ImportedFileStatus.CONFLICT.value, 0),
        "files_no_match": file_counts.get(ImportedFileStatus.NO_MATCH.value, 0),
        "files_safety_blocked": file_counts.get(ImportedFileStatus.SAFETY_BLOCKED.value, 0),
        "files_imported": file_counts.get(ImportedFileStatus.IMPORTED.value, 0),
        "files_failed": file_counts.get(ImportedFileStatus.FAILED.value, 0),
        "duplicate_files_importable": duplicate_file_counts.get(ImportedFileStatus.MATCHED.value, 0)
        + duplicate_file_counts.get(ImportedFileStatus.CONFIRMED.value, 0),
        "duplicate_files_selected": duplicate_selected_count,
        "matched_series_selected": matched_selected_series_count,
        "matched_series_importable": _object_to_int(selection_state["matched_series_importable"]),
        "duplicate_series_importable": duplicate_importable_series_count,
        "duplicate_series_selected": duplicate_selected_series_count,
        "selected_series_total": matched_selected_series_count + duplicate_selected_series_count,
        "selected_items_total": _object_to_int(selection_state["selected_item_count"])
        + story_arcs_selected,
        "importable_items_total": _object_to_int(selection_state["importable_item_count"])
        + story_arcs_reviewable,
        "resolved_file_conflict_groups": resolved_file_conflict_groups,
        "story_arcs_total": sum(story_arc_counts.values()),
        "story_arcs_detected": story_arc_counts.get(ImportedStoryArcStatus.DETECTED.value, 0),
        "story_arcs_needs_review": story_arc_counts.get(
            ImportedStoryArcStatus.NEEDS_REVIEW.value,
            0,
        ),
        "story_arcs_ready": story_arc_counts.get(ImportedStoryArcStatus.READY.value, 0),
        "story_arcs_selected": story_arcs_selected,
        "story_arcs_reviewable": story_arcs_reviewable,
        "story_arcs_skipped": story_arc_counts.get(ImportedStoryArcStatus.SKIPPED.value, 0),
        "story_arc_entries_total": sum(story_arc_entry_counts.values()),
        "story_arc_entries_resolved": story_arc_entry_counts.get(
            StoryArcResolutionState.RESOLVED.value,
            0,
        ),
        "story_arc_entries_missing": story_arc_entry_counts.get(
            StoryArcResolutionState.MISSING.value,
            0,
        ),
        "story_arc_entries_ambiguous": story_arc_entry_counts.get(
            StoryArcResolutionState.AMBIGUOUS.value,
            0,
        ),
        "story_arc_entries_conflict": story_arc_entry_counts.get(
            StoryArcResolutionState.CONFLICT.value,
            0,
        ),
        "story_arc_entries_pending": story_arc_entry_counts.get(
            StoryArcResolutionState.PENDING.value,
            0,
        ),
        "story_arc_entries_skipped": story_arc_entry_counts.get(
            StoryArcResolutionState.SKIPPED.value,
            0,
        ),
        "duplicate_files_duplicate": duplicate_file_counts.get(
            ImportedFileStatus.DUPLICATE_FILE.value,
            0,
        ),
        "duplicate_files_already_owned": duplicate_file_counts.get(
            ImportedFileStatus.ALREADY_OWNED.value,
            0,
        ),
        "duplicate_files_no_match": duplicate_file_counts.get(ImportedFileStatus.NO_MATCH.value, 0),
        "duplicate_files_conflict": duplicate_file_counts.get(ImportedFileStatus.CONFLICT.value, 0),
    }
    row_summary["series_file_conflicts"] = int(
        (
            await session.execute(
                select(func.count(ImportedSeries.id)).where(
                    ImportedSeries.import_job_id == job.id,
                    ImportedSeries.files_conflict > 0,
                )
            )
        ).scalar_one()
    )
    row_summary["series_conflicts_total"] = (
        row_summary["series_file_conflicts"] + series_candidate_conflicts
    )

    active_scan_statuses = {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.PAUSED,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
    }
    if job.status in active_scan_statuses:
        return {
            **row_summary,
            "series_total": max(row_summary["series_total"], job.series_found or 0),
            "series_in_library": max(row_summary["series_in_library"], job.series_duplicate or 0),
            "series_matched": max(row_summary["series_matched"], job.series_matched or 0),
            "series_no_match": max(row_summary["series_no_match"], job.series_no_match or 0),
            "series_imported": max(row_summary["series_imported"], job.series_imported or 0),
            "series_failed": max(row_summary["series_failed"], job.series_failed or 0),
            "files_total": max(row_summary["files_total"], job.scan_total_files or 0),
            "files_duplicate": max(
                row_summary["files_duplicate"],
                job.total_files_duplicate or 0,
            ),
            "files_already_owned": max(
                row_summary["files_already_owned"],
                job.total_files_already_owned or 0,
            ),
            "files_conflict": max(row_summary["files_conflict"], job.total_files_conflict or 0),
            "files_imported": max(row_summary["files_imported"], job.total_files_imported or 0),
            "files_failed": max(row_summary["files_failed"], job.total_files_failed or 0),
        }

    return row_summary


async def load_import_safety_failure_summary(
    session: AsyncSession,
    job: ImportJob,
) -> list[dict[str, object]]:
    """Return actionable safety/source-failure categories for import review."""
    result = await session.execute(
        select(
            ImportedFile.file_name,
            ImportedFile.diagnostics,
        ).where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status.in_([ImportedFileStatus.SAFETY_BLOCKED, ImportedFileStatus.FAILED]),
        )
    )
    failures: list[tuple[str, Mapping[str, object]]] = []
    for file_name, diagnostics in result.all():
        if not isinstance(diagnostics, Mapping):
            continue
        safety_block = diagnostics.get("safety_block")
        if isinstance(safety_block, Mapping):
            failures.append((str(file_name), safety_block))
            continue
        source_revalidation = diagnostics.get("source_revalidation")
        if isinstance(source_revalidation, Mapping):
            failures.append((str(file_name), source_revalidation))
    return summarize_import_safety_failures(failures)
