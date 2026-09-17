"""Bounded, read-only inventory of files recorded for an import series."""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics

FILES_PER_PAGE = 25


@dataclass(frozen=True, slots=True)
class InventoryFile:
    id: int
    name: str
    size: int
    folder: str
    missing_reference: bool


@dataclass(frozen=True, slots=True)
class SeriesFileInventory:
    series: ImportedSeries
    files: list[InventoryFile]
    page: int
    total_pages: int
    total: int


async def load_series_file_inventory(
    session: AsyncSession, job_id: int, series_id: int, page: int
) -> SeriesFileInventory:
    series = await session.scalar(
        select(ImportedSeries).where(
            ImportedSeries.id == series_id,
            ImportedSeries.import_job_id == job_id,
        )
    )
    if series is None:
        raise HTTPException(status_code=404, detail="Import series not found.")
    scope = (ImportedFile.import_job_id == job_id, ImportedFile.import_series_id == series_id)
    total = int(await session.scalar(select(func.count(ImportedFile.id)).where(*scope)) or 0)
    total_pages = max(1, (total + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
    page = max(1, min(page, total_pages))
    rows = await session.execute(
        select(
            ImportedFile.id,
            ImportedFile.file_name,
            ImportedFile.file_path,
            ImportedFile.file_size,
            ImportedFile.diagnostics,
        )
        .where(*scope)
        .order_by(ImportedFile.file_name, ImportedFile.id)
        .offset((page - 1) * FILES_PER_PAGE)
        .limit(FILES_PER_PAGE)
    )
    files = []
    for row in rows:
        block = (row.diagnostics or {}).get("safety_block")
        missing = (
            isinstance(block, dict)
            and normalize_import_safety_diagnostics(block)["category"] == "source_missing"
        )
        files.append(
            InventoryFile(
                id=row.id,
                name=row.file_name,
                size=row.file_size,
                folder=row.file_path.replace("\\", "/").rsplit("/", 1)[0],
                missing_reference=missing,
            )
        )
    return SeriesFileInventory(series, files, page, total_pages, total)
