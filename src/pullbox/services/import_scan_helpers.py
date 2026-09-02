"""Import scan setup and preflight helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select

from pullbox.core.file_safety import (
    FileSafetyError,
    FileSafetyInspection,
    classify_resource_safety_exception,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
    run_safety_checks,
)
from pullbox.core.source_metadata import archive_entry_issue_hint_from_names
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_content_inspection import inspect_import_content
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


FileSafetyCheck = Callable[
    ["AsyncSession", Path],
    Awaitable[FileSafetyInspection | None],
]
FileSafetyProgress = Callable[[int, int, str], Awaitable[None]]


async def reset_scan_artifacts(session: AsyncSession, job: ImportJob) -> None:
    """Clear scan-produced rows and counters before a fresh/recovered scan run."""
    staged_arc_ids = sa_select(ImportedStoryArc.id).where(ImportedStoryArc.import_job_id == job.id)
    await session.execute(
        sa_delete(ImportedStoryArcEntry).where(
            ImportedStoryArcEntry.imported_story_arc_id.in_(staged_arc_ids)
        )
    )
    await session.execute(
        sa_delete(ImportedStoryArc).where(ImportedStoryArc.import_job_id == job.id)
    )
    await session.execute(sa_delete(ImportedFile).where(ImportedFile.import_job_id == job.id))
    await session.execute(sa_delete(ImportedSeries).where(ImportedSeries.import_job_id == job.id))

    job.error_message = None
    job.progress_snapshot = {}
    job.scan_total_files = 0
    job.scan_total_dirs = 0
    job.series_found = 0
    job.series_duplicate = 0
    job.series_matched = 0
    job.series_no_match = 0
    job.series_new = 0
    job.total_files_found = 0
    job.total_files_matched = 0
    job.total_files_duplicate = 0
    job.total_files_already_owned = 0
    job.total_files_conflict = 0
    job.total_files_no_match = 0
    job.scan_started_at = None
    job.scan_completed_at = None
    job.match_started_at = None
    job.match_completed_at = None
    await session.flush()


async def _build_default_file_safety_check(session: AsyncSession) -> FileSafetyCheck:
    """Build a per-batch file safety checker with immutable config values."""
    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_archive_size = await get_archive_size_limit_bytes(session)

    async def _check_file_safety(
        _session: AsyncSession,
        path: Path,
    ) -> FileSafetyInspection:
        return await asyncio.to_thread(
            run_safety_checks,
            path,
            block_dangerous=block_dangerous,
            max_archive_size=max_archive_size,
        )

    return _check_file_safety


async def validate_discovered_files_safety(
    session: AsyncSession,
    discovered_list: list[DiscoveredSeries],
    *,
    check_file_safety: FileSafetyCheck | None = None,
    progress_callback: FileSafetyProgress | None = None,
) -> None:
    """Run safety checks once per unique discovered source file."""
    effective_check_file_safety = check_file_safety
    if effective_check_file_safety is None:
        effective_check_file_safety = await _build_default_file_safety_check(session)

    files_by_path: dict[str, list[DiscoveredFile]] = {}
    for discovered in discovered_list:
        for discovered_file in discovered.files:
            # Source containment/signature failures are never archive overrides.
            if isinstance(discovered_file.metadata_diagnostics.get("file_safety"), dict):
                continue
            files_by_path.setdefault(discovered_file.file_path, []).append(discovered_file)

    total = len(files_by_path)
    if progress_callback is not None:
        await progress_callback(0, total, "")
    for completed, (file_path, discovered_files) in enumerate(files_by_path.items(), 1):
        try:
            inspection = await effective_check_file_safety(session, Path(file_path))
            content_diagnostics = (
                await asyncio.to_thread(inspect_import_content, Path(file_path), inspection)
                if inspection is not None
                else {}
            )
        except FileSafetyError as exc:
            resource_block = classify_resource_safety_exception(exc)
            safety_block = build_import_safety_diagnostics(
                exc.reason,
                details=exc.details,
                kind=(resource_block.kind if resource_block is not None else None),
                source=(resource_block.source if resource_block is not None else "file_safety"),
                overrideable_hint=(
                    resource_block.overrideable if resource_block is not None else False
                ),
            )
            for discovered_file in discovered_files:
                metadata_diagnostics = dict(discovered_file.metadata_diagnostics)
                metadata_diagnostics["file_safety"] = safety_block
                discovered_file.metadata_diagnostics = metadata_diagnostics
        else:
            if inspection is None:
                continue
            for discovered_file in discovered_files:
                discovered_file.metadata_diagnostics = {
                    **discovered_file.metadata_diagnostics,
                    **content_diagnostics,
                }
            archive_report = next(
                (
                    report
                    for report in inspection.archives
                    if report.archive_path == Path(file_path)
                ),
                None,
            )
            if archive_report is None:
                continue
            for discovered_file in discovered_files:
                metadata_diagnostics = dict(discovered_file.metadata_diagnostics)
                if archive_report.comicinfo_entry_count == 0:
                    archive_hint = archive_entry_issue_hint_from_names(
                        list(archive_report.entry_names),
                        expected_series_name=discovered_file.parsed_series,
                    )
                    metadata_diagnostics.update(
                        {
                            "archive_metadata_loaded": True,
                            "archive_metadata_deferred": False,
                            "archive_entry_issue_hint_checked": True,
                            "has_comicinfo": False,
                        }
                    )
                    if archive_hint is not None:
                        metadata_diagnostics["archive_entry_issue_hint"] = dict(archive_hint)
                    discovered_file.metadata_diagnostics = metadata_diagnostics
                    continue
                archive_evidence: dict[str, object] = {
                    "member_index_scanned": True,
                    "comicinfo_entry_count": archive_report.comicinfo_entry_count,
                }
                if archive_report.comicinfo_entry is not None:
                    archive_evidence["comicinfo_entry"] = archive_report.comicinfo_entry
                if archive_report.comicinfo is not None:
                    archive_evidence["comicinfo"] = asdict(archive_report.comicinfo)
                if archive_report.comicinfo_error is not None:
                    archive_evidence["comicinfo_error"] = archive_report.comicinfo_error
                metadata_diagnostics["archive_member_evidence"] = archive_evidence
                discovered_file.metadata_diagnostics = metadata_diagnostics
        finally:
            if progress_callback is not None:
                await progress_callback(completed, total, file_path)
