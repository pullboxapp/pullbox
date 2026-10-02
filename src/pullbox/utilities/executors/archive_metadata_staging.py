"""Private paired CBZ output from archives/PDF for journal-owning workflows."""

import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from pullbox.core.file_safety import FileSafetyError
from pullbox.core.metadata_identity import ExternalIdentityRef
from pullbox.core.metadata_pdf_source import PdfQuality
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_writing import write_cbz_metadata
from pullbox.services.metadata_series_refresh_state import SeriesRefreshState
from pullbox.utilities.executors.archive_subprocess import (
    ControlCheck,
    ProgressCallback,
    _dispatch_progress_callback,
    _run_archive_operation,
    _write_progress_state,
)

_MAX_REQUEST_BYTES = 4 * 1024 * 1024
logger = structlog.get_logger(__name__)
type FileFingerprint = tuple[int, int, int, int, int, int]


class _StagingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: Path
    source_fingerprint: FileFingerprint
    series: MetadataSnapshot
    issue: MetadataSnapshot
    max_uncompressed_bytes: int = Field(gt=0, strict=True)
    block_dangerous: bool = Field(default=True, strict=True)
    primary_identity: ExternalIdentityRef | None = None
    previous_series: MetadataSnapshot | None = None
    previous_issue: MetadataSnapshot | None = None
    replace_managed: bool = Field(default=False, strict=True)
    pdf_quality: PdfQuality = "medium"
    metadata_state: SeriesRefreshState | None = None


class _StagingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fingerprint: FileFingerprint


class ArchiveMetadataStagingError(RuntimeError):
    """Fixed worker diagnostic, without request contents or archive metadata."""


@dataclass(frozen=True)
class StagedArchiveMetadata:
    """Short-lived evidence, not path authorization or permission to replace a file."""

    path: Path
    source_path: Path
    source_fingerprint: FileFingerprint
    output_fingerprint: FileFingerprint

    def check_unchanged(self) -> None:
        """Recheck file evidence immediately before the owner's commit boundary."""
        _check_fingerprint(self.source_path, self.source_fingerprint)
        _check_fingerprint(self.path, self.output_fingerprint)


@asynccontextmanager
async def stage_cbz_metadata_interruptible(
    source_path: Path,
    staging_parent: Path,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    *,
    max_uncompressed_bytes: int,
    block_dangerous: bool = True,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
    primary_identity: ExternalIdentityRef | None = None,
    previous_series: MetadataSnapshot | None = None,
    previous_issue: MetadataSnapshot | None = None,
    replace_managed: bool = False,
    pdf_quality: PdfQuality = "medium",
    metadata_state: SeriesRefreshState | None = None,
) -> AsyncIterator[StagedArchiveMetadata]:
    """Stage a verified pair, then yield to the independently authorized owner.

    The worker is never given a final destination and cannot replace the source.
    The caller must revalidate binding, policy and fingerprints, journal intent,
    then publish within this context. Staging is removed on exit, including
    cancellation, but never follows an artifact moved out by the owner. Use a
    staging parent on the final target's filesystem for atomic publication.
    This is not a restart journal or a lock against concurrent filesystem edits.
    """
    if cancellation_check is not None:
        await cancellation_check()
    source_path = source_path.absolute()
    request = _StagingRequest(
        source_path=source_path,
        source_fingerprint=_fingerprint(source_path),
        series=series,
        issue=issue,
        max_uncompressed_bytes=max_uncompressed_bytes,
        block_dangerous=block_dangerous,
        primary_identity=primary_identity,
        previous_series=previous_series,
        previous_issue=previous_issue,
        replace_managed=replace_managed,
        pdf_quality=pdf_quality,
        metadata_state=metadata_state,
    )
    encoded = request.model_dump_json().encode("utf-8")
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("Metadata staging request exceeds limit")
    directory = Path(tempfile.mkdtemp(prefix=".pullbox-metadata-stage-", dir=staging_parent))
    identity = directory.lstat()
    try:
        request_path = directory / "request.json"
        fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
        result = await _run_archive_operation(
            "paired_stage",
            {"request_path": str(request_path)},
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
            progress_state_path=directory / "progress.json",
        )
        try:
            # JSON transports the fixed tuple as a list; all six elements stay strict integers.
            if not isinstance(result, dict) or set(result) != {"fingerprint"}:
                raise ValueError("Invalid worker result")
            fingerprint = result["fingerprint"]
            if not isinstance(fingerprint, list):
                raise ValueError("Invalid worker fingerprint")
            completed = _StagingResult(fingerprint=tuple(fingerprint))
        except (TypeError, ValueError):
            raise ArchiveMetadataStagingError("worker_failed") from None
        output = directory / "metadata.cbz"
        prepared = StagedArchiveMetadata(
            output, source_path, request.source_fingerprint, completed.fingerprint
        )
        if progress_callback is not None:
            await _dispatch_progress_callback(progress_callback, "ready", 1, 1, "files")
        if cancellation_check is not None:
            await cancellation_check()
        prepared.check_unchanged()
        yield prepared
    finally:
        _cleanup_workspace(directory, identity)


def _fingerprint(path: Path) -> FileFingerprint:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise FileSafetyError("Metadata source/output must be a regular file")
    return _stat_fingerprint(info)


def _stat_fingerprint(info: os.stat_result) -> FileFingerprint:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _check_fingerprint(path: Path, expected: FileFingerprint) -> None:
    try:
        if _fingerprint(path) == expected:
            return
    except (FileNotFoundError, FileSafetyError):
        pass
    raise FileSafetyError("Archive changed during metadata staging")


def _cleanup_workspace(directory: Path, expected: os.stat_result) -> None:
    try:
        current = directory.lstat()
        if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        ):
            shutil.rmtree(directory)
        else:
            logger.warning("archive_metadata_workspace_changed")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("archive_metadata_workspace_cleanup_failed", errno=exc.errno)


def _read_request(path: Path) -> _StagingRequest:
    before = _fingerprint(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if _stat_fingerprint(os.fstat(stream.fileno())) != before:
            raise ValueError("Request changed")
        encoded = stream.read(_MAX_REQUEST_BYTES + 1)
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds limit")
        _check_fingerprint(path, before)
    return _StagingRequest.model_validate_json(encoded)


def worker_stage_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Internal worker entry; only fixed output names under the private workspace."""
    try:
        request_path = Path(payload["request_path"])
        request = _read_request(request_path)
    except (OSError, ValueError, TypeError, KeyError, FileSafetyError):
        raise ArchiveMetadataStagingError("invalid_plan") from None
    directory = request_path.parent
    try:
        _check_fingerprint(request.source_path, request.source_fingerprint)
    except FileSafetyError:
        raise ArchiveMetadataStagingError("source_changed") from None

    def progress(stage: str, current: int, total: int, unit: str) -> None:
        _write_progress_state(
            directory / "progress.json",
            "staging" if stage == "publishing" else stage,
            current,
            total,
            unit,
        )

    try:
        write_cbz_metadata(
            request.source_path,
            directory / "metadata.cbz",
            request.series,
            request.issue,
            max_uncompressed_bytes=request.max_uncompressed_bytes,
            block_dangerous=request.block_dangerous,
            temp_path=directory / "constructing.cbz",
            progress_callback=progress,
            primary_identity=request.primary_identity,
            previous_series=request.previous_series,
            previous_issue=request.previous_issue,
            replace_managed=request.replace_managed,
            pdf_quality=request.pdf_quality,
            metadata_state=request.metadata_state,
        )
        _check_fingerprint(request.source_path, request.source_fingerprint)
    except ArchiveMetadataRenderError:
        raise ArchiveMetadataStagingError("metadata_conflict") from None
    except (FileSafetyError, zipfile.BadZipFile):
        raise ArchiveMetadataStagingError("unsafe_archive") from None
    except (OSError, ValueError, RuntimeError):
        raise ArchiveMetadataStagingError("worker_failed") from None
    return {"fingerprint": list(_fingerprint(directory / "metadata.cbz"))}
