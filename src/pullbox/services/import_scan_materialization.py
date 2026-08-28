"""Materialize scanner results into import review rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredSeries
    from pullbox.models.import_job import ImportJob


async def materialize_discovered_scan_results(
    session: AsyncSession,
    job: ImportJob,
    discovered_list: list[DiscoveredSeries],
) -> list[tuple[DiscoveredSeries, ImportedSeries]]:
    """Persist discovered scanner output as import review series/file rows."""
    series_pairs: list[tuple[DiscoveredSeries, ImportedSeries]] = []
    for discovered in discovered_list:
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=discovered.raw_series_name,
            raw_year=discovered.raw_year,
            raw_publisher=discovered.raw_publisher,
            file_count=discovered.file_count,
            sample_paths=[str(p) for p in discovered.sample_paths],
            source_folder=discovered.source_folder,
            has_files=discovered.has_files,
            status=ImportSeriesStatus.PENDING,
            diagnostics=dict(discovered.diagnostics),
        )
        if discovered.mylar3_cv_id:
            item.cv_id = discovered.mylar3_cv_id
            item.cv_match_method = "mylar3_cv_id"
        elif discovered.folder_cv_id:
            item.cv_id = discovered.folder_cv_id
            item.cv_match_method = "folder_cv_id"
        elif discovered.comicinfo_cv_id:
            item.cv_id = discovered.comicinfo_cv_id
            item.cv_match_method = "comicinfo_cv_id"
        session.add(item)
        series_pairs.append((discovered, item))

    job.series_found = len(discovered_list)
    job.scan_completed_at = datetime.now(UTC)
    await session.flush()

    total_files = 0
    for discovered, series_item in series_pairs:
        series_file_count = 0
        for df in discovered.files:
            metadata_diagnostics = dict(df.metadata_diagnostics)
            safety_block = metadata_diagnostics.pop("file_safety", None)
            file_status = (
                ImportedFileStatus.SAFETY_BLOCKED
                if isinstance(safety_block, dict)
                else ImportedFileStatus.PENDING
            )
            diagnostics = {
                "source_issue_type": df.issue_type.value,
                "comicvine_series_id": df.comicvine_series_id,
                "series_status": df.series_status,
                "issue_count_hint": df.issue_count_hint,
                "metadata_signals": dict(df.metadata_signals),
                "source_metadata": metadata_diagnostics,
            }
            if isinstance(safety_block, dict):
                diagnostics["safety_block"] = safety_block
            error_message = safety_block.get("reason") if isinstance(safety_block, dict) else None
            file_item = ImportedFile(
                import_job_id=job.id,
                import_series_id=series_item.id,
                file_path=df.file_path,
                file_name=df.file_name,
                file_size=df.file_size,
                file_format=df.file_format,
                parsed_series=df.parsed_series,
                parsed_issue_number=df.parsed_issue_number,
                parsed_year=df.parsed_year,
                has_comicinfo=df.has_comicinfo,
                comicvine_issue_id=df.comicvine_issue_id,
                issue_number_raw=df.issue_number_raw,
                status=file_status,
                include_in_import=False,
                error_message=error_message,
                diagnostics=diagnostics,
            )
            session.add(file_item)
            series_file_count += 1
        series_item.files_total = series_file_count
        total_files += series_file_count
    if total_files:
        await session.flush()

    return series_pairs
