"""Canonical selection state for import review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.services.import_duplicates import duplicate_merge_is_actionable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_IMPORTABLE_FILE_STATUSES = (ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED)


async def load_import_review_selection_state(
    session: AsyncSession,
    job_id: int,
) -> dict[str, object]:
    """Return the single source of truth for Step 3 import selection."""
    matched_importable_result = await session.execute(
        select(ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.MATCHED,
            ImportedSeries.files_matched > 0,
        )
        .order_by(ImportedSeries.id.asc())
    )
    matched_importable_ids = [int(series_id) for series_id in matched_importable_result.scalars()]

    matched_selected_result = await session.execute(
        select(ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.MATCHED,
            ImportedSeries.files_matched > 0,
            ImportedSeries.selected_for_import.is_(True),
        )
        .order_by(ImportedSeries.id.asc())
    )
    matched_selected_ids = [int(series_id) for series_id in matched_selected_result.scalars()]

    duplicate_series_result = await session.execute(
        select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
        )
    )
    duplicate_series = list(duplicate_series_result.scalars().all())
    actionable_duplicate_ids = {
        int(series.id) for series in duplicate_series if duplicate_merge_is_actionable(series)
    }

    duplicate_file_counts_result = await session.execute(
        select(
            ImportedFile.import_series_id,
            func.count(ImportedFile.id),
            func.sum(case((ImportedFile.include_in_import.is_(True), 1), else_=0)),
        )
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id.in_(actionable_duplicate_ids or {-1}),
            ImportedFile.status.in_(_IMPORTABLE_FILE_STATUSES),
        )
        .group_by(ImportedFile.import_series_id)
    )
    duplicate_file_counts = {
        int(series_id): {
            "importable": int(importable_count or 0),
            "selected": int(selected_count or 0),
        }
        for series_id, importable_count, selected_count in duplicate_file_counts_result.all()
    }
    duplicate_importable_ids = sorted(
        series_id for series_id, counts in duplicate_file_counts.items() if counts["importable"] > 0
    )
    duplicate_selected_ids = sorted(
        series_id for series_id, counts in duplicate_file_counts.items() if counts["selected"] > 0
    )
    duplicate_selected_file_counts = {
        series_id: counts["selected"]
        for series_id, counts in duplicate_file_counts.items()
        if counts["selected"] > 0
    }

    duplicate_files_importable = sum(
        counts["importable"] for counts in duplicate_file_counts.values()
    )
    duplicate_files_selected = sum(counts["selected"] for counts in duplicate_file_counts.values())

    return {
        "matched_series_importable": len(matched_importable_ids),
        "matched_series_selected": len(matched_selected_ids),
        "duplicate_series_importable": len(duplicate_importable_ids),
        "duplicate_series_selected": len(duplicate_selected_ids),
        "duplicate_files_importable": duplicate_files_importable,
        "duplicate_files_selected": duplicate_files_selected,
        "importable_item_count": len(matched_importable_ids) + len(duplicate_importable_ids),
        "selected_item_count": len(matched_selected_ids) + len(duplicate_selected_ids),
        "selected_series_ids": matched_selected_ids,
        "selected_duplicate_series_ids": duplicate_selected_ids,
        "duplicate_selected_file_counts": duplicate_selected_file_counts,
    }
