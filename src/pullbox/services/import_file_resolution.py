"""Import file selection and issue-resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select as sa_select
from sqlalchemy.orm import joinedload

from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.services.import_file_issue_signals import candidate_issue_number_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def load_importable_files(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    duplicate_mode: bool = False,
) -> list[ImportedFile]:
    """Load files eligible for import for a reviewed series bucket."""
    file_filters = [
        ImportedFile.import_series_id == item.id,
        ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
    ]
    if duplicate_mode:
        file_filters.append(ImportedFile.include_in_import.is_(True))

    files_result = await session.execute(sa_select(ImportedFile).where(*file_filters))
    importable_files = list(files_result.scalars().all())

    return importable_files


async def load_issue_lookup_for_series(
    session: AsyncSession,
    series_id: int | None,
) -> tuple[dict[int, Issue], dict[str, Issue], dict[float, Issue]]:
    """Build ComicVine, exact-number, and unambiguous numeric lookup maps."""
    if series_id is None:
        return {}, {}, {}

    issues_result = await session.execute(
        sa_select(Issue)
        .options(joinedload(Issue.series).joinedload(Series.publisher))
        .where(Issue.series_id == series_id)
        .execution_options(populate_existing=True)
    )
    issues = issues_result.scalars().all()

    cv_id_to_issue: dict[int, Issue] = {}
    exact_number_to_issue: dict[str, Issue] = {}
    issues_by_number: dict[float, list[Issue]] = {}
    for issue in issues:
        if issue.comicvine_id is not None:
            cv_id_to_issue[issue.comicvine_id] = issue
        exact_number_to_issue[issue.effective_issue_number_text] = issue
        issues_by_number.setdefault(issue.issue_number, []).append(issue)
    number_to_issue = {
        issue_number: candidates[0]
        for issue_number, candidates in issues_by_number.items()
        if len(candidates) == 1
    }
    return cv_id_to_issue, exact_number_to_issue, number_to_issue


async def resolve_import_file_issue(
    session: AsyncSession,
    imp_file: ImportedFile,
    *,
    cv_id_to_issue: dict[int, Issue],
    exact_number_to_issue: dict[str, Issue],
    number_to_issue: dict[float, Issue],
) -> Issue | None:
    """Resolve a pre-import file match to a persisted library issue."""
    if imp_file.matched_issue_id is not None:
        resolved_issue = await session.get(Issue, imp_file.matched_issue_id)
        if resolved_issue is not None:
            return resolved_issue

    if imp_file.matched_issue_cv_id:
        resolved_issue = cv_id_to_issue.get(imp_file.matched_issue_cv_id)
        if resolved_issue is not None:
            return resolved_issue

    if imp_file.comicvine_issue_id:
        resolved_issue = cv_id_to_issue.get(imp_file.comicvine_issue_id)
        if resolved_issue is not None:
            return resolved_issue

    exact_issue_number = candidate_issue_number_text(imp_file)
    if exact_issue_number is not None:
        return exact_number_to_issue.get(exact_issue_number)

    if imp_file.parsed_issue_number is not None:
        return number_to_issue.get(imp_file.parsed_issue_number)

    return None
