"""Library browser rename helpers for immediate file and folder renames."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import or_, select

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import require_mutable_library_target
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class LibraryRenameOutcome:
    """Rename outcome returned to the API layer."""

    kind: str
    source_path: str
    target_path: str


def _rename_path(source: Path, target: Path) -> None:
    """Rename a file or folder, including case-only changes."""
    if not source.exists():
        raise ValidationError("Selected library item no longer exists on disk.")

    is_case_only = source.name.lower() == target.name.lower()
    if not is_case_only and target.exists():
        raise ValidationError("A file or folder with that name already exists.")

    if is_case_only:
        temp_path = source.parent / f".rename_tmp_{os.urandom(8).hex()}"
        source.rename(temp_path)
        temp_path.rename(target)
        return

    source.rename(target)


async def _sync_file_record(session: AsyncSession, *, before_path: str, after_path: str) -> None:
    result = await session.execute(select(LibraryFile).where(LibraryFile.file_path == before_path))
    library_file = result.scalar_one_or_none()
    if library_file is None:
        return

    updated_path = Path(after_path)
    library_file.file_path = after_path
    library_file.file_name = updated_path.name
    if updated_path.exists():
        stat = updated_path.stat()
        library_file.file_size = stat.st_size
        library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)


async def _sync_folder_records(session: AsyncSession, *, before_path: str, after_path: str) -> None:
    old_prefix = before_path.rstrip("/")
    new_prefix = after_path.rstrip("/")

    series_result = await session.execute(
        select(Series).where(or_(Series.path == old_prefix, Series.path.like(f"{old_prefix}/%")))
    )
    for series in series_result.scalars().all():
        if not series.path:
            continue
        suffix = series.path[len(old_prefix) :]
        series.path = f"{new_prefix}{suffix}"

    file_result = await session.execute(
        select(LibraryFile).where(
            or_(LibraryFile.file_path == old_prefix, LibraryFile.file_path.like(f"{old_prefix}/%"))
        )
    )
    for library_file in file_result.scalars().all():
        if not library_file.file_path:
            continue
        suffix = library_file.file_path[len(old_prefix) :]
        next_path = f"{new_prefix}{suffix}"
        library_file.file_path = next_path
        library_file.file_name = Path(next_path).name


async def rename_library_entry(
    session: AsyncSession,
    *,
    source: Path,
    target: Path,
    kind: str,
) -> LibraryRenameOutcome:
    """Rename a single Library browser target and sync tracked DB paths."""
    renamed = False

    try:
        await require_mutable_library_target(
            session,
            source,
            include_descendants=kind == "folder",
            operation="renamed",
        )
        _rename_path(source, target)
        renamed = True

        if kind == "file":
            await _sync_file_record(session, before_path=str(source), after_path=str(target))
        else:
            await _sync_folder_records(session, before_path=str(source), after_path=str(target))

        await session.commit()
        return LibraryRenameOutcome(
            kind=kind,
            source_path=str(source),
            target_path=str(target),
        )
    except ValidationError:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        restored = False
        if renamed and target.exists() and not source.exists():
            try:
                _rename_path(target, source)
                restored = True
            except Exception:
                logger.exception(
                    "library_rename_rollback_failed",
                    source_path=str(source),
                    target_path=str(target),
                )

        message = "Rename could not be completed."
        if restored:
            message += " The original name was restored."
        raise ValidationError(message) from exc
