"""Import rollback review-state helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select as sa_select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.story_arc import ImportedStoryArcStatus
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def restore_review_state_after_rollback(
    session: AsyncSession,
    job_id: int,
    *,
    batch_size: int = 500,
) -> None:
    """Restore pre-import review state using bounded keyset pages.

    Rollback can cover hundreds of thousands of staging rows. Flush each page
    so SQLAlchemy can release clean ORM identities before the next keyset page;
    the caller still owns the transaction and commit boundary.
    """
    if isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("Rollback review-state batch size must be positive")

    orphan_recovery_series_ids: set[int] = set()
    last_file_id = 0
    while True:
        imported_files = list(
            (
                await session.scalars(
                    sa_select(ImportedFile)
                    .where(
                        ImportedFile.import_job_id == job_id,
                        ImportedFile.id > last_file_id,
                    )
                    .order_by(ImportedFile.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not imported_files:
            break
        for imp_file in imported_files:
            diagnostics = dict(imp_file.diagnostics or {})
            if diagnostics.get("kind") == "orphan_recovery":
                orphan_recovery_series_ids.add(int(imp_file.import_series_id))
            _restore_imported_file_state(imp_file, diagnostics=diagnostics)
        await session.flush()
        last_file_id = int(imported_files[-1].id)

    last_series_id = 0
    while True:
        series_items = list(
            (
                await session.scalars(
                    sa_select(ImportedSeries)
                    .where(
                        ImportedSeries.import_job_id == job_id,
                        ImportedSeries.id > last_series_id,
                    )
                    .order_by(ImportedSeries.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not series_items:
            break
        for series_item in series_items:
            if series_item.status in {
                ImportSeriesStatus.IMPORTED,
                ImportSeriesStatus.FAILED,
                ImportSeriesStatus.CONFIRMED,
                ImportSeriesStatus.IMPORTING,
            }:
                series_item.status = (
                    ImportSeriesStatus.RECOVERY_PENDING
                    if series_item.id in orphan_recovery_series_ids
                    else ImportSeriesStatus.MATCHED
                )
                series_item.series_id = None
                series_item.error_message = None
                series_item.files_imported = 0
                series_item.files_failed = 0
        await session.flush()
        last_series_id = int(series_items[-1].id)

    last_arc_id = 0
    while True:
        story_arcs = list(
            (
                await session.scalars(
                    sa_select(ImportedStoryArc)
                    .where(
                        ImportedStoryArc.import_job_id == job_id,
                        ImportedStoryArc.id > last_arc_id,
                    )
                    .order_by(ImportedStoryArc.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not story_arcs:
            break
        for story_arc in story_arcs:
            if story_arc.status in {
                ImportedStoryArcStatus.IMPORTED,
                ImportedStoryArcStatus.FAILED,
            }:
                story_arc.status = (
                    ImportedStoryArcStatus.CONFIRMED
                    if story_arc.selected_for_import
                    else ImportedStoryArcStatus.SKIPPED
                )
            story_arc.materialized_story_arc_id = None
            diagnostics = dict(story_arc.diagnostics or {})
            diagnostics.pop("materialization", None)
            story_arc.diagnostics = diagnostics
        await session.flush()
        last_arc_id = int(story_arcs[-1].id)

    last_entry_id = 0
    while True:
        story_arc_entries = list(
            (
                await session.scalars(
                    sa_select(ImportedStoryArcEntry)
                    .join(
                        ImportedStoryArc,
                        ImportedStoryArcEntry.imported_story_arc_id == ImportedStoryArc.id,
                    )
                    .where(
                        ImportedStoryArc.import_job_id == job_id,
                        ImportedStoryArcEntry.id > last_entry_id,
                    )
                    .order_by(ImportedStoryArcEntry.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not story_arc_entries:
            break
        for entry in story_arc_entries:
            entry.materialized_membership_id = None
            diagnostics = dict(entry.diagnostics or {})
            diagnostics.pop("materialization", None)
            entry.diagnostics = diagnostics
        await session.flush()
        last_entry_id = int(story_arc_entries[-1].id)


def _restore_imported_file_state(
    imp_file: ImportedFile,
    *,
    diagnostics: dict[str, object],
) -> None:
    if imp_file.status not in {
        ImportedFileStatus.IMPORTED,
        ImportedFileStatus.FAILED,
        ImportedFileStatus.CONFIRMED,
        ImportedFileStatus.SKIPPED,
    }:
        return

    if diagnostics.get("kind") == "orphan_recovery":
        if diagnostics.get("resolution") == "skipped":
            imp_file.status = ImportedFileStatus.SKIPPED
        elif imp_file.matched_issue_id is not None or imp_file.matched_issue_cv_id is not None:
            imp_file.status = ImportedFileStatus.MATCHED
        else:
            imp_file.status = ImportedFileStatus.NO_MATCH
    elif imp_file.conflict_group_id is not None:
        imp_file.status = ImportedFileStatus.CONFLICT
    elif diagnostics.get("target_state") == "already_owned":
        imp_file.status = ImportedFileStatus.ALREADY_OWNED
    elif imp_file.matched_issue_id is not None or imp_file.matched_issue_cv_id is not None:
        imp_file.status = ImportedFileStatus.MATCHED
    else:
        imp_file.status = ImportedFileStatus.NO_MATCH
    if imp_file.status == ImportedFileStatus.NO_MATCH:
        diagnostics.setdefault("reason", "rollback_restored_unmatched")
        diagnostics.setdefault(
            "rejection_reason",
            "This file returned to unresolved review after the import was rolled back.",
        )
        imp_file.diagnostics = diagnostics
    imp_file.include_in_import = False
    imp_file.library_file_id = None
    imp_file.error_message = None
