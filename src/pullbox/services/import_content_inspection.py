"""Import-only comic content review, without extracting page payloads."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from pullbox.core.archive import ArchiveError, ArchiveReader
from pullbox.core.archive_format import ARCHIVE_SUFFIXES, archive_format
from pullbox.core.file_safety import FileSafetyError
from pullbox.core.nested_comics import DEFAULT_LIMIT, inspect_nested_comic
from pullbox.core.page_sources.base import canonical_page_names
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics

if TYPE_CHECKING:
    from pullbox.core.file_safety import FileSafetyInspection


logger = structlog.get_logger(__name__)


def inspect_import_content(
    path: Path, inspection: FileSafetyInspection, *, max_archive_size: int = DEFAULT_LIMIT
) -> dict[str, object]:
    """Reuse ZIP inventory; other comic archives need only their member headers.

    PDF and EPUB are not image archives and must not use this page heuristic.
    Call from a worker thread, just like archive safety inspection.
    """
    if path.suffix.lower() not in {".cbz", ".cbr", ".cb7", ".cbt"}:
        return {}
    report = next((report for report in inspection.archives if report.archive_path == path), None)
    if report is not None and report.page_count is not None:
        page_count = report.page_count
    else:
        try:
            members = ArchiveReader(path).list_members()
        except ArchiveError as exc:
            logger.warning(
                "import_archive_content_inspection_failed",
                file_name=path.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise FileSafetyError("Archive inspection failed", details=[str(path)]) from exc
        page_count = len(
            canonical_page_names(
                [
                    member.name
                    for member in members
                    if member.is_regular_file and not member.is_link and member.size > 0
                ]
            )
        )
    diagnostics: dict[str, object] = {
        "content_inspection": {"version": 1, "page_count": page_count},
        "archive_format": {
            "declared": path.suffix.lower().lstrip("."),
            "detected": archive_format(path),
            "mismatch": path.suffix.lower().lstrip(".") != archive_format(path),
        },
    }
    # Most imports contain no wrappers. Reuse the existing ZIP inventory rather
    # than opening every ordinary CBZ a second time on a large NAS library.
    nested = (
        inspect_nested_comic(path, max_bytes=max_archive_size)
        if report is None
        or any(Path(name).suffix.casefold() in ARCHIVE_SUFFIXES for name in report.entry_names)
        else None
    )
    if nested is not None:
        diagnostics["nested_comic"] = nested
        diagnostics["file_safety"] = build_import_safety_diagnostics(
            "nested_comic_archive", code="nested_comic_archive", source="import_content"
        )
    elif page_count < 2:
        code = "archive_no_pages" if page_count == 0 else "single_page_comic"
        diagnostics["file_safety"] = build_import_safety_diagnostics(
            code,
            code=code,
            kind=code,
            source="import_content",
        )
    return diagnostics
