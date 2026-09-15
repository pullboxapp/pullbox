"""Resolve staged one-page files for a read-only reader preview."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pullbox.core.exceptions import ValidationError
from pullbox.core.filesystem_policy import resolve_preview_source
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.models.library import FileFormat
from pullbox.services.import_review_file_assignment import load_review_file
from pullbox.services.import_review_recheck import _retry_source_roots
from pullbox.services.reader_content_service import (
    ReaderSourceRecord,
    ResolvedReaderSource,
    resolve_reader_source,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ImportReaderRecord:
    source: ReaderSourceRecord
    roots: tuple[Path, ...]
    signature: dict[str, Any]


async def load_import_reader_record(
    session: AsyncSession, job_id: int, file_id: int
) -> ImportReaderRecord:
    job, series, file = await load_review_file(session, job_id, file_id)
    block = file.diagnostics.get("safety_block", {})
    if block.get("category") != "single_page_comic":
        raise ValidationError("This preview is only available for one-page archive review.")
    roots = await _retry_source_roots(session, job, file_ids=[file_id])
    detected = file.diagnostics.get("archive_format", {}).get("detected")
    return ImportReaderRecord(
        source=ReaderSourceRecord(
            issue_id=-file.id,
            issue_title=Path(file.file_name).name,
            issue_number="",
            issue_number_value=0,
            series_id=-series.id,
            series_title=series.cv_title or series.raw_series_name,
            # Separate staged previews from registered library cache identities.
            library_file_id=-file.id,
            file_path=file.file_path,
            root_path="",
            file_format=FileFormat(detected or file.file_format),
            stored_file_hash=None,
        ),
        roots=tuple(roots),
        signature=dict(file.source_signature or {}),
    )


def resolve_import_reader_source(record: ImportReaderRecord) -> ResolvedReaderSource:
    """Recheck containment and scan identity outside the database session."""
    from dataclasses import replace

    raw = Path(record.source.file_path)
    path = resolve_preview_source(raw)
    lexical = raw.expanduser().absolute()
    root = next(
        (
            root
            for root in record.roots
            if lexical.is_relative_to(root) and path.is_relative_to(root.resolve(strict=True))
        ),
        None,
    )
    if root is None:
        raise ValidationError("The file is outside this import's approved source folders.")
    validate_file_identity_signature(record.signature, build_file_identity_signature(path))
    return resolve_reader_source(replace(record.source, root_path=str(root)))
