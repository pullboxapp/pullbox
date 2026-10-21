"""Keep post-import user edits out of historical import rollback."""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models import Issue, LibraryFile
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
)
from pullbox.models.library_conversion import LibraryConversion

EDIT_KEY = "post_import_edit"
PRESERVED_EDIT_MESSAGE = (
    "Preserved because these comic files were edited after import. "
    "Their files and library records were kept; no repair is needed."
)


async def lock_import_file_history(session: AsyncSession, file_id: int) -> None:
    """Use the import-owner order before locking the library binding."""
    jobs = select(ImportedFile.import_job_id).where(ImportedFile.library_file_id == file_id)
    await session.execute(
        select(ImportJob.id).where(ImportJob.id.in_(jobs)).order_by(ImportJob.id).with_for_update()
    )
    await session.execute(
        select(ImportedFile.id)
        .where(ImportedFile.library_file_id == file_id)
        .order_by(ImportedFile.id)
        .with_for_update()
    )


async def record_post_import_edit(
    session: AsyncSession, file_id: int, *, kind: str, operation_id: UUID
) -> None:
    """Commit the last successful edit with file accounting, never replace source proof."""
    file = await session.get(LibraryFile, file_id)
    if file is None or file.issue_id is None:
        return
    issue = await session.get(Issue, file.issue_id)
    if issue is None:
        return
    evidence = {
        "schema_version": 1,
        "library_file_id": file.id,
        "issue_id": issue.id,
        "series_id": issue.series_id,
        "kind": kind,
        "operation_id": str(operation_id),
    }
    cursor = 0
    while True:
        rows = list(
            await session.scalars(
                select(ImportedFile)
                .where(ImportedFile.library_file_id == file_id, ImportedFile.id > cursor)
                .order_by(ImportedFile.id)
                .limit(128)
            )
        )
        if not rows:
            return
        for row in rows:
            diagnostics = dict(row.diagnostics or {})
            details = diagnostics.get("comicinfo_enrichment")
            if isinstance(details, dict) and details.get("status") in {"pending", "failed"}:
                diagnostics["comicinfo_enrichment"] = {
                    **details,
                    "status": "superseded",
                    "reason": "post_import_edit",
                }
            row.diagnostics = {**diagnostics, EDIT_KEY: evidence}
        cursor = rows[-1].id


async def preserve_edited_import_action(session: AsyncSession, action: ImportJobAction) -> bool:
    """Preserve changed files and dependent series state, including renamed folders."""
    if action.status is ImportJobActionStatus.ROLLED_BACK:
        return False
    payload = action.payload
    query = select(ImportedFile.id).where(ImportedFile.import_job_id == action.import_job_id)
    if action.action_type in {"library_file_registered", "library_file_placement_started"}:
        imported_id, file_id = payload.get("imported_file_id"), payload.get("library_file_id")
        if type(imported_id) is int and imported_id > 0:
            query = query.where(ImportedFile.id == imported_id)
        elif type(file_id) is int and file_id > 0:
            query = query.where(
                or_(
                    ImportedFile.library_file_id == file_id,
                    ImportedFile.diagnostics[EDIT_KEY]["library_file_id"].as_integer() == file_id,
                )
            )
        else:
            return False
    elif action.action_type.startswith("series_"):
        series_id = payload.get("series_id")
        if type(series_id) is not int or series_id <= 0:
            return False
        query = query.join(ImportedSeries).where(
            or_(
                ImportedSeries.series_id == series_id,
                ImportedFile.diagnostics[EDIT_KEY]["series_id"].as_integer() == series_id,
            )
        )
    else:
        return False
    await session.execute(
        select(ImportJob.id).where(ImportJob.id == action.import_job_id).with_for_update()
    )
    edited = await session.scalar(
        query.where(ImportedFile.diagnostics[EDIT_KEY]["schema_version"].as_integer() == 1).limit(1)
    )
    if edited is None:
        files = query.with_only_columns(ImportedFile.library_file_id)
        publication = await session.scalar(
            select(ArchiveMetadataPublication.id)
            .where(ArchiveMetadataPublication.active_file_id.in_(files))
            .limit(1)
        )
        conversion = await session.scalar(
            select(LibraryConversion.id)
            .where(LibraryConversion.library_file_id.in_(files), LibraryConversion.active.is_(True))
            .limit(1)
        )
        if publication is None and conversion is None:
            return False
    action.status = ImportJobActionStatus.ROLLBACK_FAILED
    action.error_message = (
        PRESERVED_EDIT_MESSAGE
        if edited is not None
        else (
            "A file update still needs to finish or recover. Pullbox preserved its file and "
            "series folder; retry rollback after the update settles."
        )
    )
    action.rolled_back_at = None
    await session.flush()
    return True
