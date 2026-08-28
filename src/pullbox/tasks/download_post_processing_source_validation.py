"""Source resolution, safety, and integrity checks for post-processing."""

from __future__ import annotations

import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from pullbox.core.file_safety import (
    FileSafetyError,
    classify_resource_safety_exception,
    get_allowed_extensions,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
    run_safety_checks,
)
from pullbox.tasks.post_processing_progress import PostProcessingPhase
from pullbox.utilities.executors.integrity_checker import check_file_integrity


@dataclass(frozen=True)
class SourceValidationResult:
    """Resolved source state after safety and integrity checks."""

    source_dir: Path
    source_exists: bool
    probe_root: Path
    comic_file: Path | None
    attempts: int


ResolveLocalPath = Callable[..., Awaitable[str | None]]
ProbeSource = Callable[..., Awaitable[Any]]
BuildIntegrityException = Callable[[Path, list[str]], Exception]


async def resolve_and_validate_source(
    *,
    session: Any,
    download: Any,
    trace: Any,
    runtime: Any,
    log: Any,
    resolve_local_path: ResolveLocalPath,
    probe_source: ProbeSource,
    build_integrity_exception: BuildIntegrityException,
    allow_resource_safety_exception: bool = False,
) -> SourceValidationResult:
    """Resolve the source path, probe shared storage, and validate the comic file."""
    from asyncio import get_running_loop

    local_path = await resolve_local_path(session, download)
    if not local_path:
        raise FileNotFoundError(
            "Download client did not report a file path. "
            "Check that the download completed successfully."
        )

    source_dir = Path(local_path)
    trace.source_path = str(source_dir)
    log.debug("post_processing_resolved_path", local_path=str(source_dir))

    allowed_exts = await get_allowed_extensions(session)
    source_probe = await probe_source(source_dir, allowed_exts)
    source_exists = source_probe.source_seen
    probe_root = source_probe.probe_root
    trace.probe_root = str(probe_root)
    comic_file = source_probe.comic_file

    if probe_root.expanduser().resolve(strict=False) == Path("/"):
        log.error(
            "post_processing_root_probe_rejected",
            raw_client_path=getattr(download, "downloaded_path", None),
            hint="Verify Remote Path and Download Directory before retrying post-processing.",
        )
        raise RuntimeError(
            "Refusing to inspect the container filesystem root during post-processing. "
            "Verify the download client's Remote Path and Download Directory."
        )

    if comic_file is None:
        # Don't fail immediately; a prior failed attempt may have already moved
        # the file to the destination. The caller will check dest_path later.
        log.warning(
            "post_processing_source_missing",
            path=str(probe_root),
            raw_client_path=(
                None
                if getattr(download.download_client, "value", None) == "airdcpp"
                else download.downloaded_path
            ),
            attempts=source_probe.attempts,
            hint=(
                "Completed download folder was visible, but no readable comic file "
                "appeared during the retry window. This can mean an incomplete or "
                "bad release, or delayed shared-storage visibility. Will also check "
                "if the file already reached the destination."
            ),
        )
    elif source_probe.attempts > 1:
        log.info(
            "post_processing_source_recovered",
            path=str(comic_file),
            probe_root=str(probe_root),
            attempts=source_probe.attempts,
            hint="Source became visible after retrying the shared-storage probe.",
        )

    if comic_file is not None:
        runtime.enter_phase(PostProcessingPhase.VALIDATING_FILES)
        block_dangerous = await is_dangerous_file_blocking_enabled(session)
        max_archive_size = await get_archive_size_limit_bytes(session)

        safety_start = _time.monotonic()
        try:
            await get_running_loop().run_in_executor(
                None,
                partial(
                    run_safety_checks,
                    probe_root,
                    block_dangerous=block_dangerous,
                    max_archive_size=max_archive_size,
                ),
            )
        except FileSafetyError as exc:
            resource_block = classify_resource_safety_exception(exc)
            if allow_resource_safety_exception and resource_block is not None:
                log.warning(
                    "post_processing_resource_safety_allowed_once",
                    kind=resource_block.kind,
                    reason=resource_block.reason,
                )
            else:
                log.error(
                    "post_processing_safety_rejected",
                    reason=exc.reason,
                    details=exc.details,
                )
                raise RuntimeError(f"File safety: {exc.reason}") from exc
        except NotADirectoryError:
            # Network mounts (NFS/SMB) can raise ENOTDIR during safety checks.
            # Log a warning but proceed; transfer still succeeds or fails.
            log.warning(
                "post_processing_safety_enotdir",
                path=str(probe_root),
                hint="Safety check hit ENOTDIR on network mount — proceeding with transfer.",
            )
        finally:
            trace.safety_ms = round((_time.monotonic() - safety_start) * 1000, 1)

    if comic_file is not None:
        file_size = await get_running_loop().run_in_executor(
            None, lambda: comic_file.stat().st_size if comic_file.exists() else 0
        )
        trace.file_size_bytes = file_size
        log.debug(
            "post_processing_file_found",
            file_name=comic_file.name,
            file_size_bytes=file_size,
            extension=comic_file.suffix,
        )

        integrity_start = _time.monotonic()
        integrity_result = await check_file_integrity(comic_file, deep=False)
        trace.integrity_ms = round((_time.monotonic() - integrity_start) * 1000, 1)
        if integrity_result.status == "corrupt":
            log.error(
                "post_processing_integrity_failed",
                path=str(comic_file),
                errors=integrity_result.errors,
                warnings=integrity_result.warnings,
            )
            raise build_integrity_exception(
                comic_file,
                integrity_result.errors,
            )
        if integrity_result.warnings:
            log.warning(
                "post_processing_integrity_warning",
                path=str(comic_file),
                warnings=integrity_result.warnings,
                page_count=integrity_result.page_count,
            )

    if comic_file is None and source_exists:
        raise FileNotFoundError(
            f"No readable comic file was found in the completed download folder "
            f"{probe_root} after {source_probe.attempts} checks. This usually means "
            "the release is incomplete or bad, or the shared download mount has not "
            "exposed the file yet. Try another release. If multiple releases fail, "
            "verify Remote Path, Download Directory, and Pullbox access to /downloads."
        )

    return SourceValidationResult(
        source_dir=source_dir,
        source_exists=source_exists,
        probe_root=probe_root,
        comic_file=comic_file,
        attempts=source_probe.attempts,
    )
