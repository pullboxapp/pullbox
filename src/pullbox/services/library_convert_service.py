"""Library browser helpers for immediate single-file conversions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import require_mutable_library_target
from pullbox.models.library import FileFormat, LibraryFile
from pullbox.utilities.executors.file_converter import convert_file
from pullbox.utilities.settings import move_file_to_utility_trash, restore_file_from_utility_trash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class LibraryConvertOutcome:
    """Immediate convert outcome returned to the API layer."""

    kind: str
    source_path: str
    target_path: str
    original_trash_path: str


async def _sync_converted_file_record(
    session: AsyncSession,
    *,
    before_path: str,
    after_path: str,
    metadata_embedded: bool = False,
) -> None:
    result = await session.execute(select(LibraryFile).where(LibraryFile.file_path == before_path))
    library_file = result.scalar_one_or_none()
    if library_file is None:
        return

    updated_path = Path(after_path)
    library_file.file_path = after_path
    library_file.file_name = updated_path.name
    library_file.file_format = FileFormat.CBZ
    if metadata_embedded:
        library_file.has_comicinfo = True
    if updated_path.exists():
        stat = updated_path.stat()
        library_file.file_size = stat.st_size
        library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def _conversion_error_message(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "Selected library item no longer exists on disk."
    if isinstance(exc, FileExistsError):
        return "A CBZ file with that name already exists."
    if isinstance(exc, ValueError):
        return str(exc)
    return "Conversion could not be completed."


async def convert_library_file(
    session: AsyncSession,
    *,
    source: Path,
    trash_dir: Path,
    trash_relative_path: str | Path,
) -> LibraryConvertOutcome:
    """Convert a single Library browser file immediately and sync tracked DB metadata."""
    converted_path: Path | None = None
    trash_path: Path | None = None

    try:
        await require_mutable_library_target(
            session,
            source,
            include_descendants=False,
            operation="converted",
        )
        converted_path = await convert_file(source, "cbz")
        trash_path = move_file_to_utility_trash(
            source,
            trash_dir,
            relative_path=trash_relative_path,
        )
        await _sync_converted_file_record(
            session,
            before_path=str(source),
            after_path=str(converted_path),
            metadata_embedded=False,
        )
        await session.commit()
        return LibraryConvertOutcome(
            kind="file",
            source_path=str(source),
            target_path=str(converted_path),
            original_trash_path=str(trash_path),
        )
    except ValidationError:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()

        restored = False
        if trash_path is not None and trash_path.exists() and not source.exists():
            try:
                restore_file_from_utility_trash(
                    trash_path,
                    source,
                    converted_path=converted_path,
                )
                restored = True
            except Exception:
                logger.exception(
                    "library_convert_rollback_failed",
                    source_path=str(source),
                    target_path=str(converted_path) if converted_path is not None else None,
                    trash_path=str(trash_path),
                )
        elif converted_path is not None and converted_path.exists():
            converted_path.unlink()

        message = _conversion_error_message(exc)
        if restored and message == "Conversion could not be completed.":
            message += " The original file was restored."
        raise ValidationError(message) from exc
