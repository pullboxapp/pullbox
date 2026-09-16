"""Destination planning helpers for download post-processing."""

from __future__ import annotations

from asyncio import get_running_loop
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pullbox.core.library_root_resolution import preferred_managed_root_id
from pullbox.models.library import LibraryFileStorageMode, MatchConfidence
from pullbox.tasks.post_processing_progress import PostProcessingPhase


@dataclass(frozen=True)
class DestinationPlan:
    """Resolved destination path state before transfer or recovery checks."""

    dest_path: Path
    dest_dir: Path
    issue_filename: str
    extension: str


@dataclass(frozen=True)
class ExistingDestinationFile:
    """A non-empty file already present at the expected library destination."""

    path: Path
    size_bytes: int


ResolveLibraryDestination = Callable[..., Awaitable[tuple[Path, Any]]]
RegisterLibraryFile = Callable[..., Awaitable[Any]]

_COMIC_EXTENSIONS = ("cbz", "cbr", "cb7", "cbt", "pdf", "epub")


async def build_destination_plan(
    *,
    session: Any,
    comic_file: Path | None,
    series: Any,
    issue: Any,
    log: Any,
    resolve_library_destination: ResolveLibraryDestination,
) -> DestinationPlan:
    """Resolve and normalize the destination path for post-processing."""
    # When source is missing from a prior failed move, default to cbz and rely
    # on the later recovery path to find an alternate existing extension.
    extension = comic_file.suffix.lstrip(".").lower() if comic_file else "cbz"
    destination_probe = comic_file or Path(f"{series.title} #probe.{extension}")
    destination_root_id = preferred_managed_root_id(series)
    dest_path, _resolved_root = await resolve_library_destination(
        session,
        destination_probe,
        issue,
        library_root_id=destination_root_id,
    )
    dest_dir = dest_path.parent
    issue_filename = dest_path.name

    # Safety: ensure we have an absolute path.
    if not dest_dir.is_absolute():
        log.warning(
            "post_processing_relative_dest",
            dest_dir=str(dest_dir),
            hint="Destination directory is relative. Resolving against CWD. "
            "Set PULLBOX_LIBRARY_ROOT to an absolute path.",
        )
        dest_dir = dest_dir.resolve()

    dest_path = dest_dir / issue_filename

    log.debug(
        "post_processing_transfer_plan",
        source=str(comic_file),
        destination=str(dest_path),
        library_root_id=destination_root_id,
    )

    return DestinationPlan(
        dest_path=dest_path,
        dest_dir=dest_dir,
        issue_filename=issue_filename,
        extension=extension,
    )


async def find_existing_destination_file(
    *,
    comic_file: Path | None,
    dest_path: Path,
    dest_dir: Path,
    log: Any,
) -> ExistingDestinationFile | None:
    """Find a file left at the destination by a prior partial post-process attempt."""
    dest_exists = await get_running_loop().run_in_executor(None, dest_path.exists)

    # If exact path doesn't exist and we guessed the extension (source missing),
    # try to find a file with any comic extension at the expected stem.
    if not dest_exists and comic_file is None:
        stem = dest_path.stem
        for alt_ext in _COMIC_EXTENSIONS:
            alt_path = dest_dir / f"{stem}.{alt_ext}"
            if await get_running_loop().run_in_executor(None, alt_path.exists):
                dest_path = alt_path
                dest_exists = True
                log.debug(
                    "post_processing_found_alt_extension",
                    dest_path=str(dest_path),
                    extension=alt_ext,
                )
                break

    source_and_dest_same = (
        dest_exists and comic_file is not None and comic_file.resolve() == dest_path.resolve()
    )
    if not dest_exists or source_and_dest_same:
        return None

    dest_stat = await get_running_loop().run_in_executor(None, dest_path.stat)
    if dest_stat.st_size <= 0:
        return None

    log.debug(
        "post_processing_already_at_destination",
        dest_path=str(dest_path),
        dest_size=dest_stat.st_size,
        hint="File already exists at destination — skipping transfer. "
        "Likely from a prior attempt that moved the file but failed "
        "on metadata.",
    )
    return ExistingDestinationFile(path=dest_path, size_bytes=dest_stat.st_size)


async def register_existing_destination_file(
    *,
    session: Any,
    existing_destination: ExistingDestinationFile,
    issue: Any,
    series: Any,
    download: Any,
    trace: Any,
    runtime: Any,
    log: Any,
    register_library_file: RegisterLibraryFile,
) -> Path:
    """Register a file already moved to its library destination by a prior attempt."""
    dest_path = existing_destination.path
    trace.final_path = str(dest_path)
    trace.file_size_bytes = existing_destination.size_bytes
    trace.transferred_bytes = existing_destination.size_bytes
    download.final_path = str(dest_path)

    runtime.enter_phase(PostProcessingPhase.REGISTERING_LIBRARY_FILE)
    await register_library_file(
        session,
        dest_path,
        issue,
        MatchConfidence.HIGH,
        move_to_library=False,
        storage_mode=LibraryFileStorageMode.MANAGED,
        recover_existing_managed_artifact=True,
        rename=False,
        library_root_id=preferred_managed_root_id(series),
    )
    trace.finalize_current_phase()
    log.info(
        "post_processing_complete",
        final_path=str(dest_path),
        file_size=existing_destination.size_bytes,
    )
    runtime.emit_summary(outcome="success")
    return dest_path
