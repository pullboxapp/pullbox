"""Explicit per-file reassignment without modifying source files."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_review_scope import review_scope
from pullbox.services.import_story_arc_resolution import refresh_story_arc_entries_for_import_files

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.metadata_service import MetadataService


async def load_review_file(
    session: AsyncSession, job_id: int, file_id: int
) -> tuple[ImportJob, ImportedSeries, ImportedFile]:
    job = await session.get(ImportJob, job_id)
    file = await session.get(ImportedFile, file_id)
    if job is None or file is None or file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)
    if (
        job.status is not ImportJobStatus.REVIEW
        or job.control_request is not ImportControlRequest.NONE
    ):
        raise ValidationError("This import is no longer available for review.")
    parent = await session.get(ImportedSeries, file.import_series_id)
    if parent is None or parent.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", file.import_series_id)
    if parent.diagnostics.get("rematch_pending"):
        raise ValidationError("Wait for this series to finish preparing its match.")
    return job, parent, file


async def assign_review_file(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    cv_id: int,
    issue_cv_id: int,
    metadata_service: MetadataService,
) -> ImportedFile:
    """Change one staged identity; never move a file or rewrite its source metadata."""
    job, parent, file = await load_review_file(session, job_id, file_id)
    original_scope = review_scope(job, parent, file)
    if file.status not in {
        ImportedFileStatus.CONFLICT,
        ImportedFileStatus.NO_MATCH,
        ImportedFileStatus.MATCHED,
        ImportedFileStatus.CONFIRMED,
    }:
        raise ValidationError("Resolve source inspection before assigning this file.")
    signals = file.diagnostics.get("metadata_signals", {})
    if (
        signals.get("comicvine_issue_id") in {"comicinfo", "sidecar"}
        and file.comicvine_issue_id
        and file.comicvine_issue_id != issue_cv_id
    ):
        raise ValidationError(
            "The embedded ComicVine identity identifies a different issue. "
            "Correct that source evidence and recheck it first."
        )
    series_metadata = await metadata_service.get_series_metadata(cv_id)
    summaries = await metadata_service.get_issue_summaries_for_series(cv_id)
    await session.refresh(job)
    await session.refresh(parent)
    await session.refresh(file)
    if review_scope(job, parent, file) != original_scope or parent.diagnostics.get(
        "rematch_pending"
    ):
        raise ValidationError(
            "This review changed while the issue choices were loading. Try again."
        )
    summary = next((value for value in summaries if value.provider_id == str(issue_cv_id)), None)
    if str(series_metadata.provider_id) != str(cv_id) or summary is None:
        raise ValidationError("The chosen issue does not belong to the selected series.")
    library_series = await session.scalar(select(Series).where(Series.comicvine_id == cv_id))
    issue = await session.scalar(select(Issue).where(Issue.comicvine_id == issue_cv_id))
    if issue is not None and (library_series is None or issue.series_id != library_series.id):
        raise ValidationError("The saved issue belongs to a different library series.")
    owned = (
        issue is not None
        and await session.scalar(select(LibraryFile.id).where(LibraryFile.issue_id == issue.id))
        is not None
    )
    target = await session.scalar(
        select(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.cv_id == cv_id,
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
        )
        .order_by(ImportedSeries.id)
        .limit(1)
    )
    if target is not None and target.diagnostics.get("rematch_pending"):
        raise ValidationError("Wait for the destination series to finish preparing its match.")
    if target is None:
        target = ImportedSeries(
            import_job_id=job_id,
            raw_series_name=series_metadata.title,
            raw_year=series_metadata.year_start,
            cv_id=cv_id,
            user_selected_cv_id=cv_id,
            cv_title=series_metadata.title,
            cv_year=series_metadata.year_start,
            cv_publisher=series_metadata.publisher,
            cv_issue_count=series_metadata.issue_count,
            cv_url=series_metadata.comicvine_url,
            cv_match_method="user_override",
            cv_match_score=1.0,
            source_folder=parent.source_folder,
            sample_paths=[file.file_path],
            has_files=True,
            status=ImportSeriesStatus.DUPLICATE if library_series else ImportSeriesStatus.MATCHED,
            series_id=library_series.id if library_series else None,
            selected_for_import=not owned,
            diagnostics={
                "kind": "duplicate_series" if library_series else "manual_file_assignment"
            },
        )
        session.add(target)
        await session.flush()
    other_targets = list(
        (
            await session.scalars(
                select(ImportedFile).where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.import_series_id == target.id,
                    ImportedFile.id != file.id,
                    ImportedFile.matched_issue_cv_id == issue_cv_id,
                    ImportedFile.status.in_(
                        [
                            ImportedFileStatus.MATCHED,
                            ImportedFileStatus.CONFIRMED,
                            ImportedFileStatus.CONFLICT,
                        ]
                    ),
                )
            )
        ).all()
    )
    old_series_id = parent.id
    file.import_series_id = target.id
    file.matched_issue_cv_id = issue_cv_id
    file.matched_issue_id = issue.id if issue is not None else None
    file.status = ImportedFileStatus.ALREADY_OWNED if owned else ImportedFileStatus.MATCHED
    file.include_in_import = not owned
    file.match_method = "import_reconcile"
    file.match_confidence = "manual"
    file.conflict_group_id = None
    file.duplicate_group_id = None
    file.duplicate_of_file_id = None
    file.is_preferred = False
    file.error_message = None
    file.diagnostics = {
        **file.diagnostics,
        "review_selection": not owned,
        "target_issue_summary": asdict(summary),
        "target_state": "already_owned" if owned else "missing",
        "manual_file_assignment": {
            "source_series_id": old_series_id,
            "target_series_id": target.id,
            "target_series_cv_id": cv_id,
            "target_issue_cv_id": issue_cv_id,
        },
    }
    # An explicit reassignment cannot silently choose between two files for one issue.
    if other_targets and not owned:
        group_id = (
            int(
                await session.scalar(
                    select(func.max(ImportedFile.conflict_group_id)).where(
                        ImportedFile.import_job_id == job_id
                    )
                )
                or 0
            )
            + 1
        )
        for value in [file, *other_targets]:
            value.status = ImportedFileStatus.CONFLICT
            value.conflict_group_id = group_id
            value.include_in_import = False
            value.is_preferred = False
    await session.flush()
    await recompute_file_counters(session, job, series_ids=list({old_series_id, target.id}))
    for row in (parent, target):
        row.file_count = row.files_total
        if not row.files_total:
            row.status = ImportSeriesStatus.SKIPPED
            row.selected_for_import = False
    await recompute_series_counters(session, job)
    await refresh_story_arc_entries_for_import_files(
        session, import_job_id=job_id, import_file_ids=[file.id]
    )
    await session.flush()
    return file
