"""File staging and ComicInfo helpers for import execution."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pullbox.core.archive import inspect_archive_page_count as inspect_archive_page_count
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.file_safety import is_resource_safety_exception_allowed
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz
from pullbox.utilities.comicinfo_creators import load_comicinfo_creator_fields
from pullbox.utilities.executors.file_converter import convert_file

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportJob
    from pullbox.models.issue import Issue


ProgressCallback = Callable[[str, int, int, str], Awaitable[None] | None]
FileConverter = Callable[..., Awaitable[Path]]
ComicInfoEmbedder = Callable[..., Any]


@dataclass(slots=True)
class PreparedImportFile:
    """Staged file inputs needed for safe import execution and rollback."""

    registration_source: Path
    original_source: Path
    cleanup_paths: list[Path]
    original_trash_path: Path | None = None
    converted: bool = False
    skip_embedded_comicinfo: bool = False
    preparation_warning: str | None = None


async def prepare_import_file(
    job: ImportJob,
    imp_file: ImportedFile,
    *,
    converter: FileConverter = convert_file,
    progress_callback: ProgressCallback | None = None,
) -> PreparedImportFile:
    """Stage a source file for import, including optional conversion."""
    source_path = Path(imp_file.file_path)
    needs_cbz_normalization = job.move_to_library and (
        job.convert_to_preferred_format or job.update_embedded_comicinfo_from_match
    )
    if not needs_cbz_normalization:
        return PreparedImportFile(
            registration_source=source_path,
            original_source=source_path,
            cleanup_paths=[],
            converted=False,
        )

    if source_path.suffix.lower().lstrip(".") == "cbz":
        return PreparedImportFile(
            registration_source=source_path,
            original_source=source_path,
            cleanup_paths=[],
            converted=False,
        )

    effective_transfer_method = job.effective_transfer_method or job.transfer_method
    if effective_transfer_method not in {"move", "copy"}:
        raise ValidationError("CBZ normalization requires the transfer method to be Move or Copy.")

    temp_dir = Path(tempfile.mkdtemp(prefix="pullbox-import-convert-"))
    try:
        converter_kwargs: dict[str, Any] = {"progress_callback": progress_callback}
        if is_resource_safety_exception_allowed(imp_file.diagnostics):
            converter_kwargs["allow_resource_safety_exception"] = True
        converted_path = await converter(
            source_path,
            "cbz",
            temp_dir,
            **converter_kwargs,
        )
    except Exception:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
        raise

    return PreparedImportFile(
        registration_source=converted_path,
        original_source=source_path,
        cleanup_paths=[temp_dir],
        converted=True,
    )


def apply_comicinfo_to_imported_artifact(
    artifact_path: Path,
    comicinfo_payload: dict[str, Any],
    *,
    embedder: ComicInfoEmbedder = embed_comicinfo_in_cbz,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Write authoritative ComicInfo.xml to a final imported library artifact."""
    if artifact_path.suffix.lower() != ".cbz":
        raise ValidationError(
            "Updating embedded ComicInfo.xml from matched issue requires the final "
            "imported artifact to be a CBZ archive."
        )
    embedder(
        artifact_path,
        comicinfo_payload,
        progress_callback=progress_callback,
    )


def cleanup_prepared_file(prepared: PreparedImportFile) -> None:
    """Remove temporary staging directories/files created during import prep."""
    for cleanup_path in prepared.cleanup_paths:
        if cleanup_path.is_dir():
            shutil.rmtree(cleanup_path, ignore_errors=True)
        elif cleanup_path.exists():
            cleanup_path.unlink(missing_ok=True)


def format_comicinfo_issue_number(issue_number: float | None) -> str | None:
    """Render an issue number for ComicInfo.xml output."""
    if issue_number is None:
        return None
    return format_issue_number(issue_number)


async def build_comicinfo_payload_for_issue(
    session: AsyncSession,
    issue: Issue,
    *,
    page_count: int | None = None,
) -> dict[str, Any]:
    """Build authoritative ComicInfo.xml fields for a chosen issue."""
    series = issue.__dict__.get("series")
    if series is None:
        series = await session.get(Series, issue.series_id)
    if series is None:
        raise NotFoundError("Series", issue.series_id)

    publisher_name: str | None = None
    publisher = series.__dict__.get("publisher")
    if publisher is not None:
        publisher_name = publisher.name
    elif series.publisher_id is not None:
        publisher = await session.get(Publisher, series.publisher_id)
        if publisher is not None:
            publisher_name = publisher.name

    release_date = issue.release_date
    notes: str | None = None
    if series.comicvine_id and issue.comicvine_id:
        notes = f"[cv_vol_id:{series.comicvine_id}] [cv_issue_id:{issue.comicvine_id}]"
    elif series.comicvine_id:
        notes = f"[cv_vol_id:{series.comicvine_id}]"

    payload: dict[str, Any] = {
        "Series": series.title,
        "Number": format_comicinfo_issue_number(issue.issue_number),
        "Title": issue.title,
        "Summary": issue.description,
        "Publisher": publisher_name,
        "Year": release_date.year if release_date is not None else None,
        "Month": release_date.month if release_date is not None else None,
        "Day": release_date.day if release_date is not None else None,
        "PageCount": page_count
        if page_count and page_count > 0
        else issue.page_count
        if issue.page_count and issue.page_count > 0
        else None,
        "Count": series.issue_count if series.issue_count and series.issue_count > 0 else None,
        "Volume": series.year_start,
        "Web": issue.comicvine_url,
        "Notes": notes,
    }
    payload.update(await load_comicinfo_creator_fields(session, issue.id))
    return payload


def repaired_cbz_output_path(source_path: Path) -> Path:
    """Return a unique sibling path for a repaired CBZ copy."""
    candidate = source_path.with_name(f"{source_path.stem} [Pullbox Repaired].cbz")
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = source_path.with_name(f"{source_path.stem} [Pullbox Repaired {counter}].cbz")
        if not candidate.exists():
            return candidate
        counter += 1


async def rewrite_import_file_comicinfo(
    imp_file: ImportedFile,
    comicinfo_payload: dict[str, Any],
    *,
    converter: FileConverter = convert_file,
    embedder: ComicInfoEmbedder = embed_comicinfo_in_cbz,
    progress_callback: ProgressCallback | None = None,
) -> tuple[Path, str, str]:
    """Repair an imported archive's ComicInfo.xml, normalizing to CBZ when needed."""
    source_path = Path(imp_file.file_path)
    if source_path.suffix.lower() == ".cbz":
        await asyncio.to_thread(
            embedder,
            source_path,
            comicinfo_payload,
            progress_callback=progress_callback,
        )
        return source_path, "in_place", str(source_path)

    temp_dir = Path(tempfile.mkdtemp(prefix="pullbox-import-repair-"))
    try:
        converted_path = await converter(
            source_path,
            "cbz",
            temp_dir,
            progress_callback=progress_callback,
        )
        await asyncio.to_thread(
            embedder,
            converted_path,
            comicinfo_payload,
            progress_callback=progress_callback,
        )
        repaired_target = repaired_cbz_output_path(source_path)
        await asyncio.to_thread(shutil.move, str(converted_path), str(repaired_target))
        return repaired_target, "normalized_to_cbz", str(source_path)
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
