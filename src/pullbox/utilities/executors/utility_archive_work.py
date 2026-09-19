"""Interruptible disposable archive stages; never interrupt source publication."""

from __future__ import annotations

import asyncio
import zipfile
from typing import TYPE_CHECKING, Any

from pullbox.utilities.cancellation import (
    cancellation_enabled,
    check_archive_progress,
    check_cancelled,
    check_cancelled_async,
)

if TYPE_CHECKING:
    from pathlib import Path


def convert_utility_file(
    source: Path,
    target_format: str,
    target: Path,
    *,
    pdf_quality: str = "medium",
) -> Path:
    """Convert only into disposable output, using a killable child when queued."""
    from pullbox.utilities.executors.archive_subprocess import convert_file_interruptible
    from pullbox.utilities.executors.file_converter import _convert_sync

    check_cancelled()
    if cancellation_enabled():
        return asyncio.run(
            convert_file_interruptible(
                source,
                target_format,
                output_path=target,
                pdf_quality=pdf_quality,
                cancellation_check=check_cancelled_async,
            )
        )
    return _convert_sync(
        source,
        target_format,
        target,
        pdf_quality=pdf_quality,
        progress_callback=check_archive_progress,
    )


def embed_utility_metadata(target: Path, metadata: dict[str, Any]) -> None:
    """Rewrite a converted disposable output, not a live source archive."""
    from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz
    from pullbox.utilities.executors.archive_subprocess import embed_comicinfo_in_cbz_interruptible

    check_cancelled()
    if cancellation_enabled():
        asyncio.run(
            embed_comicinfo_in_cbz_interruptible(
                target,
                metadata,
                cancellation_check=check_cancelled_async,
            )
        )
    else:
        embed_comicinfo_in_cbz(target, metadata, progress_callback=check_archive_progress)


def verify_utility_archive(target: Path) -> int:
    """Validate CRCs with bounded reads so cancellation need not wait for a whole archive."""
    with zipfile.ZipFile(target, "r") as archive:
        if cancellation_enabled():
            for entry in archive.infolist():
                check_cancelled()
                with archive.open(entry) as stream:
                    while stream.read(1024 * 1024):
                        check_cancelled()
        else:
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"Integrity check failed: corrupt entry '{bad}'")
        return len(archive.namelist())
