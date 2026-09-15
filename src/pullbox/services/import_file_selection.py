"""Explicit review choices without changing legacy matched-file defaults."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)


def not_excluded_from_review() -> ColumnElement[bool]:
    """Old rows with no explicit choice retain their existing eligibility."""
    return func.coalesce(ImportedFile.diagnostics["review_selection"].as_boolean(), True).is_(True)


def set_review_file_selection(file: ImportedFile, selected: bool) -> None:
    file.include_in_import = selected
    file.diagnostics = {**dict(file.diagnostics or {}), "review_selection": selected}


async def defer_excluded_review_files(session: AsyncSession, job_id: int) -> set[int]:
    """At confirmation, retain opted-out files as actionable Follow-up decisions."""
    files = list(
        (
            await session.scalars(
                select(ImportedFile).where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.status.in_(
                        [ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]
                    ),
                    ImportedFile.diagnostics["review_selection"].as_boolean().is_(False),
                )
            )
        ).all()
    )
    affected = {file.import_series_id for file in files}
    for file in files:
        file.status = ImportedFileStatus.NO_MATCH
        file.diagnostics = {**file.diagnostics, "review_deferred": True}
    if affected:
        selected_parents = set(
            (
                await session.scalars(
                    select(ImportedFile.import_series_id)
                    .where(
                        ImportedFile.import_series_id.in_(affected),
                        ImportedFile.status.in_(
                            [ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]
                        ),
                        not_excluded_from_review(),
                    )
                    .distinct()
                )
            ).all()
        )
        parents = (
            await session.scalars(select(ImportedSeries).where(ImportedSeries.id.in_(affected)))
        ).all()
        for parent in parents:
            if (
                parent.status in {ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE}
                and parent.id not in selected_parents
            ):
                parent.status = ImportSeriesStatus.RECOVERY_PENDING
                parent.selected_for_import = False
    await session.flush()
    return affected
