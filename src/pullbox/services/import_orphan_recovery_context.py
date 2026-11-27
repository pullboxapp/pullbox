"""Context builders for delayed unmatched import recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.issue_numbers import format_issue_number, parse_issue_number_text
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
from pullbox.models.series import Series
from pullbox.services.import_file_issue_signals import recovery_issue_number_text
from pullbox.services.import_mylar_followup import (
    has_mylar_conflict,
    needs_mylar_conflict_review,
    recorded_mylar_series,
    registered_mylar_series,
    review_rows,
)
from pullbox.services.import_mylar_native_identity import (
    load_mylar_native_owners,
    recorded_native_refs,
)
from pullbox.services.import_orphans import is_active_orphan_row, requires_orphan_issue_decision
from pullbox.services.import_recovery_identity import conflicting_comicinfo_series

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
        raise ValidationError("Import follow-up is only available for completed imports.")
    has_live_issue_recovery = False
    if item.status == ImportSeriesStatus.IMPORTED or (
        item.status == ImportSeriesStatus.DUPLICATE
        and item.cv_match_method in {"metron_id", "gcd_id", "library_review"}
    ):
        has_live_issue_recovery = bool(
            await session.scalar(
                select(ImportedFile.id).where(
                    ImportedFile.import_series_id == item.id,
                    ImportedFile.status == ImportedFileStatus.NO_MATCH,
                )
            )
        )
    if item.status == ImportSeriesStatus.DUPLICATE and recorded_mylar_series(job, item):
        has_live_issue_recovery = await has_mylar_conflict(session, item.id)
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

    selected_title = (
        await session.scalar(select(Series.title).where(Series.id == item.series_id))
        if item.series_id is not None
        else item.cv_title
    )
    if await needs_mylar_conflict_review(session, job, item):
        conflicts = await review_rows(session, item)
        disputed_files = [file for file, _ in conflicts]
        owners = await load_mylar_native_owners(session, disputed_files)
        files_view, remaining, completed = _build_recovery_file_rows(disputed_files, [])
        for view, (file, issue_id) in zip(files_view, conflicts, strict=True):
            view["recorded_issue_id"] = issue_id
            linked_issues = sorted(
                {
                    (ref[0], owner.issue_id)
                    for ref in recorded_native_refs(file)
                    if ref[1] == "issue"
                    and (owner := owners.get(ref)) is not None
                    and owner.issue_id is not None
                    and owner.issue_id != issue_id
                }
            )
            view["provider_issues"] = [
                {"provider": provider, "library_issue_id": linked_id}
                for provider, linked_id in linked_issues
            ]
        return {
            "imported_series": item,
            "selected_series_title": selected_title,
            "mylar_conflicts_only": True,
            "issue_options": [],
            "files": files_view,
            "requires_library_root": False,
            "selected_library_root_id": job.target_library_root_id,
            "available_library_roots": [],
            "files_remaining": remaining,
            "files_completed": completed,
        }

    files_result = await session.execute(
        select(ImportedFile)
        .where(ImportedFile.import_series_id == item.id)
        .order_by(ImportedFile.id.asc())
        .execution_options(populate_existing=True)
    )
    files = list(files_result.scalars().all())

    from pullbox.services.import_metron_matching import native_review_source
    from pullbox.services.import_native_recovery import native_recovery_row

    native = native_review_source(job, item) and native_recovery_row(item)
    issue_options = (
        [] if native else await _load_recovery_issue_options(session, item, metadata_service)
    )
    file_rows, files_remaining, files_completed = _build_recovery_file_rows(
        files, issue_options, selected_title=selected_title
    )

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
        "selected_series_title": selected_title,
        "native_recovery": native,
        "can_skip_without_assignment": registered_mylar_series(job, item),
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
                    "issue_number_text": issue.issue_number_text
                    or format_issue_number(issue.issue_number),
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
                "issue_number_text": summary.issue_number_text
                or format_issue_number(summary.issue_number),
                "title": summary.title,
                "release_date": summary.release_date,
                "already_imported": False,
            }
        )
    return issue_options


def _build_recovery_file_rows(
    files: list[ImportedFile],
    issue_options: list[dict[str, Any]],
    *,
    selected_title: str | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    issue_label_by_cv_id: dict[int, str] = {}
    issue_number_to_cv_ids: dict[str, list[int]] = {}
    for option in issue_options:
        issue_cv_id = int(option["issue_cv_id"])
        issue_number = option.get("issue_number_text") or format_issue_number(
            option["issue_number"]
        )
        label = f"#{issue_number}"
        if option.get("title"):
            label = f"{label} - {option['title']}"
        issue_label_by_cv_id[issue_cv_id] = label
        try:
            numeric, exact = parse_issue_number_text(issue_number)
        except ValueError:
            continue
        if numeric == option["issue_number"]:
            issue_number_to_cv_ids.setdefault(exact, []).append(issue_cv_id)

    file_rows: list[dict[str, Any]] = []
    files_remaining = 0
    files_completed = 0
    for imp_file in files:
        file_number = recovery_issue_number_text(imp_file, imp_file.parsed_issue_number)
        current_issue_cv_id = (
            imp_file.matched_issue_cv_id
            if imp_file.matched_issue_cv_id in issue_label_by_cv_id
            else None
        )
        suggested_issue_cv_id = current_issue_cv_id
        if suggested_issue_cv_id is None and imp_file.comicvine_issue_id in issue_label_by_cv_id:
            suggested_issue_cv_id = imp_file.comicvine_issue_id
        conflicting_title = None
        if (
            suggested_issue_cv_id is None
            and file_number is not None
            and len(issue_number_to_cv_ids.get(file_number, [])) == 1
        ):
            if selected_title:
                conflicting_title = conflicting_comicinfo_series(imp_file, selected_title)
            if conflicting_title is None:
                suggested_issue_cv_id = issue_number_to_cv_ids[file_number][0]

        # A terminal retry has already checked these IDs and numbers together.
        # Keep the stored evidence, but never preselect a target it refused.
        previous_review = (imp_file.diagnostics or {}).get("completed_import_follow_up")
        if (
            imp_file.status is ImportedFileStatus.NO_MATCH
            and isinstance(previous_review, dict)
            and previous_review.get("resolution") == "issue_decision_required"
        ):
            suggested_issue_cv_id = None

        decision_locked = not requires_orphan_issue_decision(imp_file)
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
                "parsed_issue_number_text": file_number,
                "parsed_year": imp_file.parsed_year,
                "comicvine_issue_id": imp_file.comicvine_issue_id,
                "status": imp_file.status,
                "error_message": imp_file.error_message,
                "matched_issue_cv_id": current_issue_cv_id,
                "suggested_issue_cv_id": suggested_issue_cv_id,
                "conflicting_comicinfo_series": conflicting_title,
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
