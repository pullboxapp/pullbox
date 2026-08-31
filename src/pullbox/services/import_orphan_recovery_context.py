"""Context builders for delayed unmatched import recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.services.import_orphans import is_active_orphan_row

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.metadata_service import MetadataService


async def load_orphan_recovery_item(
    session: AsyncSession,
    imported_series_id: int,
) -> tuple[ImportJob, ImportedSeries]:
    """Load the completed import job + orphaned series row for delayed recovery."""
    item = await session.get(ImportedSeries, imported_series_id)
    if item is None:
        raise NotFoundError("ImportedSeries", imported_series_id)

    job = await session.get(ImportJob, item.import_job_id)
    if job is None:
        raise NotFoundError("ImportJob", item.import_job_id)
    if job.status != ImportJobStatus.COMPLETED:
        raise ValidationError("Unmatched recovery is only available for completed imports.")
    has_live_issue_recovery = False
    if item.status == ImportSeriesStatus.IMPORTED:
        has_live_issue_recovery = bool(
            await session.scalar(
                select(ImportedFile.id).where(
                    ImportedFile.import_series_id == item.id,
                    ImportedFile.status == ImportedFileStatus.NO_MATCH,
                )
            )
        )
    if not is_active_orphan_row(item) and not has_live_issue_recovery:
        raise ValidationError("This import group is not in the active unmatched queue.")
    return job, item


async def build_orphan_recovery_context(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    metadata_service: MetadataService,
) -> dict[str, Any]:
    """Build the delayed unmatched-recovery context for one orphaned series row."""
    if item.cv_id is None and item.series_id is None:
        raise ValidationError("Choose a ComicVine match before starting recovery.")

    files_result = await session.execute(
        select(ImportedFile)
        .where(ImportedFile.import_series_id == item.id)
        .order_by(ImportedFile.id.asc())
    )
    files = list(files_result.scalars().all())

    issue_options = await _load_recovery_issue_options(session, item, metadata_service)
    file_rows, files_remaining, files_completed = _build_recovery_file_rows(files, issue_options)

    available_library_roots: list[dict[str, Any]] = []
    selected_library_root_id = job.target_library_root_id
    requires_library_root = job.target_library_root_id is None and item.series_id is None
    if requires_library_root:
        roots_result = await session.execute(
            select(LibraryRoot).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id)
        )
        available_library_roots = [
            {"id": root.id, "name": root.name, "path": root.path}
            for root in roots_result.scalars().all()
        ]
        if selected_library_root_id is None and available_library_roots:
            selected_library_root_id = int(available_library_roots[0]["id"])

    return {
        "imported_series": item,
        "issue_options": issue_options,
        "files": file_rows,
        "requires_library_root": requires_library_root,
        "selected_library_root_id": selected_library_root_id,
        "available_library_roots": available_library_roots,
        "files_remaining": files_remaining,
        "files_completed": files_completed,
    }


async def _load_recovery_issue_options(
    session: AsyncSession,
    item: ImportedSeries,
    metadata_service: MetadataService,
) -> list[dict[str, Any]]:
    issue_options: list[dict[str, Any]] = []
    if item.series_id is not None:
        issues_result = await session.execute(
            select(Issue)
            .options(joinedload(Issue.library_file))
            .where(Issue.series_id == item.series_id)
            .order_by(Issue.issue_number)
        )
        for issue in issues_result.scalars().all():
            if issue.comicvine_id is None:
                continue
            issue_options.append(
                {
                    "issue_cv_id": issue.comicvine_id,
                    "issue_number": issue.issue_number,
                    "title": issue.title,
                    "release_date": (
                        issue.release_date.isoformat() if issue.release_date is not None else None
                    ),
                    "cover_url": issue.cover_url,
                    "issue_type": issue.issue_type.value,
                    "already_imported": issue.library_file is not None,
                }
            )
        return issue_options

    if item.cv_id is None:
        raise ValidationError("Choose a ComicVine match before starting recovery.")
    summaries = await metadata_service.get_issue_summaries_for_series(item.cv_id)
    for summary in summaries:
        if not summary.provider_id:
            continue
        issue_options.append(
            {
                "issue_cv_id": int(summary.provider_id),
                "issue_number": summary.issue_number,
                "title": summary.title,
                "release_date": summary.release_date,
                "already_imported": False,
            }
        )
    return issue_options


def _build_recovery_file_rows(
    files: list[ImportedFile],
    issue_options: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    issue_label_by_cv_id: dict[int, str] = {}
    issue_number_to_cv_ids: dict[float, list[int]] = {}
    for option in issue_options:
        issue_cv_id = int(option["issue_cv_id"])
        issue_number = float(option["issue_number"])
        label = f"#{format_issue_number(issue_number)}"
        if option.get("title"):
            label = f"{label} - {option['title']}"
        issue_label_by_cv_id[issue_cv_id] = label
        issue_number_to_cv_ids.setdefault(issue_number, []).append(issue_cv_id)

    file_rows: list[dict[str, Any]] = []
    files_remaining = 0
    files_completed = 0
    for imp_file in files:
        current_issue_cv_id = (
            imp_file.matched_issue_cv_id
            if imp_file.matched_issue_cv_id in issue_label_by_cv_id
            else None
        )
        suggested_issue_cv_id = current_issue_cv_id
        if suggested_issue_cv_id is None and imp_file.comicvine_issue_id in issue_label_by_cv_id:
            suggested_issue_cv_id = imp_file.comicvine_issue_id
        if (
            suggested_issue_cv_id is None
            and imp_file.parsed_issue_number is not None
            and len(issue_number_to_cv_ids.get(imp_file.parsed_issue_number, [])) == 1
        ):
            suggested_issue_cv_id = issue_number_to_cv_ids[imp_file.parsed_issue_number][0]

        decision_locked = imp_file.status in {
            ImportedFileStatus.IMPORTED,
            ImportedFileStatus.SKIPPED,
        }
        if decision_locked:
            files_completed += 1
        else:
            files_remaining += 1

        file_rows.append(
            {
                "imported_file_id": imp_file.id,
                "file_name": imp_file.file_name,
                "file_path": imp_file.file_path,
                "file_format": imp_file.file_format,
                "parsed_issue_number": imp_file.parsed_issue_number,
                "parsed_year": imp_file.parsed_year,
                "comicvine_issue_id": imp_file.comicvine_issue_id,
                "status": imp_file.status,
                "error_message": imp_file.error_message,
                "matched_issue_cv_id": current_issue_cv_id,
                "suggested_issue_cv_id": suggested_issue_cv_id,
                "suggested_issue_label": (
                    issue_label_by_cv_id.get(suggested_issue_cv_id)
                    if suggested_issue_cv_id is not None
                    else None
                ),
                "decision_locked": decision_locked,
                "diagnostics": dict(imp_file.diagnostics or {}),
            }
        )
    return file_rows, files_remaining, files_completed
