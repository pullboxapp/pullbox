"""Interruptible file-operation adapters for import execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.utilities.executors.archive_subprocess import (
    convert_file_interruptible as default_convert_file_interruptible,
)
from pullbox.utilities.executors.archive_subprocess import (
    embed_comicinfo_in_cbz_interruptible as default_embed_comicinfo_in_cbz_interruptible,
)
from pullbox.utilities.executors.archive_subprocess import (
    materialize_cbz_with_comicinfo_interruptible as default_materialize_cbz,
)
from pullbox.utilities.executors.archive_subprocess import (
    transfer_file_interruptible as default_transfer_file_interruptible,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    ProgressCallback = Callable[[str, int, int, str], Awaitable[None] | None]
    RaiseIfCancelledImmediately = Callable[[AsyncSession, int], Awaitable[None]]
    ConvertFileInterruptible = Callable[..., Awaitable[Path]]
    EmbedComicInfoInterruptible = Callable[..., Awaitable[bool]]
    TransferFileInterruptible = Callable[..., Awaitable[Path]]
    MaterializeCbzWithComicInfoInterruptible = Callable[..., Awaitable[bool]]


async def convert_import_file_interruptible(
    session: AsyncSession,
    job: Any,
    source_path: Path,
    target_format: str,
    *,
    destination: Path | None,
    progress_callback: ProgressCallback | None = None,
    allow_resource_safety_exception: bool = False,
    raise_if_cancelled_immediately: RaiseIfCancelledImmediately,
    converter: ConvertFileInterruptible | None = None,
) -> Path:
    """Run a killable archive conversion for one active Step 4 file."""
    selected_converter = converter or default_convert_file_interruptible
    return await selected_converter(
        source_path,
        target_format,
        destination=destination,
        cancellation_check=lambda: raise_if_cancelled_immediately(session, int(job.id)),
        progress_callback=progress_callback,
        allow_resource_safety_exception=allow_resource_safety_exception,
    )


async def embed_import_comicinfo_interruptible(
    session: AsyncSession,
    job: Any,
    artifact_path: Path,
    comicinfo_payload: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
    raise_if_cancelled_immediately: RaiseIfCancelledImmediately,
    embedder: EmbedComicInfoInterruptible | None = None,
) -> bool:
    """Run a killable ComicInfo archive rewrite for one active Step 4 file."""
    selected_embedder = embedder or default_embed_comicinfo_in_cbz_interruptible
    return await selected_embedder(
        artifact_path,
        comicinfo_payload,
        cancellation_check=lambda: raise_if_cancelled_immediately(session, int(job.id)),
        progress_callback=progress_callback,
    )


async def transfer_import_artifact_interruptible(
    session: AsyncSession,
    job: Any,
    source_path: Path,
    target_path: Path,
    transfer_method: str,
    *,
    transfer_progress_callback: ProgressCallback | None = None,
    raise_if_cancelled_immediately: RaiseIfCancelledImmediately,
    transfer: TransferFileInterruptible | None = None,
) -> Path:
    """Run a killable library transfer for one active Step 4 file."""
    selected_transfer = transfer or default_transfer_file_interruptible
    return await selected_transfer(
        source_path,
        target_path,
        transfer_method,
        cancellation_check=lambda: raise_if_cancelled_immediately(session, int(job.id)),
        progress_callback=transfer_progress_callback,
    )


async def materialize_import_cbz_with_comicinfo_interruptible(
    session: AsyncSession,
    job: Any,
    source_path: Path,
    target_path: Path,
    comicinfo_payload: dict[str, Any],
    *,
    transfer_method: str,
    temp_path: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    raise_if_cancelled_immediately: RaiseIfCancelledImmediately,
    materializer: MaterializeCbzWithComicInfoInterruptible | None = None,
) -> bool:
    """Run a killable combined CBZ materialization and ComicInfo write."""
    selected_materializer = materializer or default_materialize_cbz
    return await selected_materializer(
        source_path,
        target_path,
        comicinfo_payload,
        transfer_method=transfer_method,
        temp_path=temp_path,
        cancellation_check=lambda: raise_if_cancelled_immediately(session, int(job.id)),
        progress_callback=progress_callback,
    )
