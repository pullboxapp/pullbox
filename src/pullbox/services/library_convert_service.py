"""Library browser helpers for immediate single-file conversions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.models.library import FileFormat, LibraryFile
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    read_archive_metadata_binding,
    require_unowned_metadata_file,
)
from pullbox.services.library_conversion_files import prepare_conversion
from pullbox.services.library_conversion_recovery import (
    publish_conversion,
    read_conversion_binding,
    record_conversion,
    recover_conversion,
)
from pullbox.services.library_mutation_coordination import (
    finish_short_mutation,
    lock_file_mutation_admission,
    require_no_archive_publication,
)
from pullbox.utilities.executors.archive_metadata_staging import ArchiveMetadataStagingError
from pullbox.utilities.settings import build_trash_destination

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
    restore_has_comicinfo: bool | None = None,
) -> None:
    result = await session.execute(select(LibraryFile).where(LibraryFile.file_path == before_path))
    library_file = result.scalar_one_or_none()
    if library_file is None:
        return

    updated_path = Path(after_path)
    updated_format = FileFormat(updated_path.suffix.lstrip(".").casefold())
    library_file.file_path = after_path
    library_file.file_name = updated_path.name
    library_file.file_format = updated_format
    library_file.file_hash = None
    if restore_has_comicinfo is not None:
        library_file.has_comicinfo = restore_has_comicinfo
    elif metadata_embedded:
        library_file.has_comicinfo = True
    if updated_path.exists():
        stat = updated_path.stat()
        library_file.file_size = stat.st_size
        library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def _conversion_error_message(exc: Exception) -> str:
    if isinstance(exc, ArchiveMetadataBindingError):
        if exc.code == "import_rollback_protected":
            return (
                "This file still belongs to an import's rollback journal. "
                "Paired conversion is not available for it yet; the file has not been changed."
            )
        return (
            "This comic's metadata match needs review before conversion. "
            "Review the issue match, then retry."
        )
    if isinstance(exc, ArchiveMetadataStagingError):
        if str(exc) == "metadata_conflict":
            return (
                "The comic's embedded metadata disagrees with its library metadata. "
                "Review the file metadata before converting."
            )
        if str(exc) == "unsafe_archive":
            return (
                "The source could not pass archive safety checks. "
                "Replace or repair it before converting."
            )
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
    operation_id: UUID | None = None,
    require_paired_metadata: bool = False,
) -> LibraryConvertOutcome:
    """Own the conversion session lifecycle, retaining recoverable public artifacts."""
    if session.new or session.dirty or session.deleted or session.in_nested_transaction():
        raise ValidationError("Conversion requires a clean session.")
    source = source.absolute()
    operation_id = operation_id or uuid4()
    target = source.with_suffix(".cbz")
    relative = Path(trash_relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("Invalid conversion trash location.")
    backup = build_trash_destination(trash_dir.absolute(), source, relative_path=relative)
    backup = backup.parent / f"conversion-{operation_id.hex}" / backup.name
    try:
        await lock_file_mutation_admission(session)
        await require_no_archive_publication(
            session, source, target, backup, include_descendants=False
        )
        binding = await read_conversion_binding(session, source)
        metadata_state = None
        limit = None
        block_dangerous = True
        if (
            (require_paired_metadata or get_settings().metadata_paired_conversion_writer_enabled)
            and binding.file_id is not None
            and binding.issue_id is not None
        ):
            await require_unowned_metadata_file(session, binding.file_id)
            captured = await read_archive_metadata_binding(
                session,
                binding.file_id,
                expected_issue_id=binding.issue_id,
                allow_conversion_source=True,
            )
            metadata_state = captured.metadata
            limit = await get_archive_size_limit_bytes(session)
            block_dangerous = await is_dangerous_file_blocking_enabled(session)
        if require_paired_metadata and metadata_state is None:
            raise ValidationError("Paired conversion requires a verified library issue match.")
        if target.exists() or target.is_symlink() or target == source:
            raise FileExistsError
        await session.commit()
        async with prepare_conversion(
            source,
            backup,
            binding,
            metadata_state=metadata_state,
            max_uncompressed_bytes=limit,
            block_dangerous=block_dangerous,
        ) as plan:
            await record_conversion(session, plan, operation_id)
            await finish_short_mutation(asyncio.create_task(session.commit()))
            await publish_conversion(session, operation_id)
            await finish_short_mutation(asyncio.create_task(session.commit()))
            state = await recover_conversion(session, operation_id)
            if state != "complete":
                raise ValidationError(
                    "Conversion needs review. The original and recovery evidence were preserved."
                )
        return LibraryConvertOutcome("file", str(source), str(target), str(backup))
    except BaseException as exc:
        await session.rollback()
        if isinstance(exc, ValidationError) or not isinstance(exc, Exception):
            raise
        logger.warning(
            "library_conversion_interrupted", operation_id=str(operation_id), exc_info=True
        )
        raise ValidationError(_conversion_error_message(exc)) from exc
