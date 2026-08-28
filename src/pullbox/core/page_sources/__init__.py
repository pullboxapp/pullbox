"""Format-neutral comic-reader page-source factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.core.page_sources.archive import ArchivePageSource
from pullbox.core.page_sources.base import (
    PageDescriptor,
    PagePayload,
    PageSource,
    PageSourceError,
    PageSourceErrorCode,
    ReaderResourceLimits,
    canonical_page_names,
    detect_comic_format,
)
from pullbox.core.page_sources.pdf import PdfPageSource
from pullbox.models.library import FileFormat

if TYPE_CHECKING:
    from pathlib import Path

SUPPORTED_READER_FORMATS = frozenset(
    {FileFormat.CBZ, FileFormat.CBR, FileFormat.CB7, FileFormat.CBT, FileFormat.PDF}
)
_ARCHIVE_FORMATS = SUPPORTED_READER_FORMATS - {FileFormat.PDF}


def open_page_source(
    path: Path,
    *,
    declared_format: FileFormat,
    limits: ReaderResourceLimits | None = None,
) -> PageSource:
    """Open a detected, declared, bounded comic page source."""
    active_limits = limits or ReaderResourceLimits()
    detected = detect_comic_format(path)
    if detected is not declared_format:
        raise PageSourceError(
            PageSourceErrorCode.FORMAT_MISMATCH,
            "The comic contents do not match its recorded format.",
        )
    if detected in _ARCHIVE_FORMATS:
        return ArchivePageSource(path, detected, active_limits)
    if detected is FileFormat.PDF:
        return PdfPageSource(path, active_limits)
    raise PageSourceError(
        PageSourceErrorCode.UNSUPPORTED_FORMAT,
        "This comic format is not supported by the reader.",
    )


__all__ = [
    "SUPPORTED_READER_FORMATS",
    "PageDescriptor",
    "PagePayload",
    "PageSource",
    "PageSourceError",
    "PageSourceErrorCode",
    "ReaderResourceLimits",
    "canonical_page_names",
    "detect_comic_format",
    "open_page_source",
]
