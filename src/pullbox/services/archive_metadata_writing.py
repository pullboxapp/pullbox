"""Single-pass CBZ metadata materialization for independently authorized callers."""

import os
import stat
import tempfile
import zipfile
from collections.abc import Callable
from copy import copy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import structlog

from pullbox.core.archive_metadata import read_open_metadata_members
from pullbox.core.file_publication import publish_file_without_overwrite
from pullbox.core.file_safety import (
    DANGEROUS_EXTENSIONS,
    FileSafetyError,
    has_archive_member_path_traversal,
)
from pullbox.core.metadata_archive_source import MetadataArchiveSource, open_metadata_archive
from pullbox.core.metadata_identity import ExternalIdentityRef
from pullbox.core.metadata_pdf_source import PdfQuality
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_binding import (
    assemble_archive_metadata_state,
    select_archive_primary_identity,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import render_archive_metadata
from pullbox.services.metadata_series_refresh_state import SeriesRefreshState

ArchiveMetadataProgress = Callable[[str, int, int, str], None]
_CHUNK_BYTES = 1024 * 1024
_METADATA_NAMES = {"comicinfo.xml", "metroninfo.xml"}
logger = structlog.get_logger(__name__)


def write_cbz_metadata(
    source_path: Path,
    target_path: Path,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    *,
    max_uncompressed_bytes: int,
    replace_source: bool = False,
    block_dangerous: bool = True,
    temp_path: Path | None = None,
    progress_callback: ArchiveMetadataProgress | None = None,
    check_cancelled: Callable[[], None] | None = None,
    primary_identity: ExternalIdentityRef | None = None,
    previous_series: MetadataSnapshot | None = None,
    previous_issue: MetadataSnapshot | None = None,
    replace_managed: bool = False,
    pdf_quality: PdfQuality = "medium",
    metadata_state: SeriesRefreshState | None = None,
) -> bool:
    """Write a verified pair in one CBZ construction, off the event loop.

    Paths must already be authorized and file/issue/parent binding verified by
    the workflow. This primitive grants no managed ownership and does not remove
    a copied source. Callers retain journal/rollback and per-file serialization.
    Only an explicit same-path refresh may replace a file; other destinations
    use exclusive publication. Cooperative cancellation ends before publication.
    """
    if isinstance(max_uncompressed_bytes, bool) or max_uncompressed_bytes <= 0:
        raise ValueError("Archive budget must be positive")
    if pdf_quality not in {"high", "medium", "low"}:
        raise ValueError("Invalid PDF quality preset")
    source_path = source_path.absolute()
    target_path = target_path.absolute()
    refresh = source_path == target_path
    if replace_source != refresh:
        raise ValueError("Source replacement must explicitly target the same path")
    if target_path.suffix.casefold() != ".cbz":
        raise ValueError("Metadata output requires a CBZ target")
    if temp_path is not None:
        temp_path = temp_path.absolute()
        if temp_path.parent.resolve() != target_path.parent.resolve():
            raise ValueError("Temporary output must share the target directory")
        if temp_path in {source_path, target_path}:
            raise ValueError("Temporary output must not be the source or target")
    if not refresh and os.path.lexists(target_path):
        raise FileExistsError(target_path)
    _check_cancelled(check_cancelled)
    before = source_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise FileSafetyError("Metadata source must be a regular file, not a link")
    if refresh and not before.st_mode & 0o222:
        raise FileSafetyError("Cannot replace a read-only source archive")
    fd = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as source_stream:
        _check_source(source_path, source_stream, before)
        with open_metadata_archive(
            source_stream,
            source_path,
            limit=max_uncompressed_bytes,
            scratch_parent=target_path.parent,
            validate=lambda entries: _validate_members(
                entries, max_uncompressed_bytes, block_dangerous
            ),
            check_cancelled=lambda: _check_cancelled(check_cancelled),
            progress=lambda current, total: _progress(
                progress_callback, check_cancelled, "extracting", current, total
            ),
            pdf_quality=pdf_quality,
            pdf_progress=lambda current, total: _progress(
                progress_callback, check_cancelled, "rendering", current, total, "pages"
            ),
        ) as source:
            entries = source.entries
            files = read_open_metadata_members(entries, source.open)
            if metadata_state is not None:
                if (
                    len(metadata_state.issues) != 1
                    or metadata_state.series.identities != series.identities
                    or metadata_state.issues[0].identities != issue.identities
                ):
                    raise ValueError("Conversion metadata disagrees with its verified binding")
                archive = reconcile_archive_metadata(files)
                series, issue = assemble_archive_metadata_state(
                    metadata_state, archive, now=datetime.now(UTC)
                )
                primary_identity = select_archive_primary_identity(metadata_state, archive)
                previous_series = metadata_state.series.baseline
                previous_issue = metadata_state.issues[0].baseline
            pair = render_archive_metadata(
                series,
                issue,
                files,
                primary_identity=primary_identity,
                previous_series=previous_series,
                previous_issue=previous_issue,
                replace_managed=replace_managed,
            )
            outputs = {"ComicInfo.xml": pair.comicinfo, "MetronInfo.xml": pair.metroninfo}
            members = [entry for entry in entries if not _metadata_member(entry)]
            if (
                refresh
                and files.comicinfo.payload == pair.comicinfo
                and files.metroninfo.payload == pair.metroninfo
                and {entry.filename for entry in entries if _metadata_member(entry)} == set(outputs)
            ):
                _check_cancelled(check_cancelled)
                _check_source(source_path, source_stream, before)
                return False
            total_bytes = sum(entry.file_size for entry in members) + sum(
                map(len, outputs.values())
            )
            if total_bytes > max_uncompressed_bytes:
                raise FileSafetyError(
                    "Archive decompressed size exceeds limit after metadata rendering"
                )
            _check_cancelled(check_cancelled)
            stage, stage_fd = _create_stage(target_path, temp_path)
            stage_identity = os.fstat(stage_fd)
            try:
                with os.fdopen(stage_fd, "w+b") as output_stream:
                    manifest = _construct(
                        source,
                        output_stream,
                        members,
                        outputs,
                        total_bytes,
                        progress_callback,
                        check_cancelled,
                    )
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                _verify(
                    stage, manifest, source.comment, total_bytes, progress_callback, check_cancelled
                )
                current_stage = stage.lstat()
                if (current_stage.st_dev, current_stage.st_ino) != (
                    stage_identity.st_dev,
                    stage_identity.st_ino,
                ):
                    raise FileSafetyError("Temporary archive changed before publication")
                if refresh:
                    # Do not copy ownership/xattrs/timestamps: NAS mounts may prohibit them.
                    stage.chmod(stat.S_IMODE(before.st_mode) & 0o777)
                ready = stage.lstat()
                _check_source(source_path, source_stream, before)
                # Windows will not replace an open source. All input is consumed now.
                source.close()
                source_stream.close()
                _progress(progress_callback, check_cancelled, "publishing", 0, 1, "files")
                _check_source(source_path, None, before)
                if _fingerprint(stage.lstat()) != _fingerprint(ready):
                    raise FileSafetyError("Temporary archive changed before publication")
                if refresh:
                    os.replace(stage, target_path)
                else:
                    try:
                        publish_file_without_overwrite(stage, target_path)
                    except OSError:
                        # The link fallback can publish successfully before unlink fails.
                        # Only our verified inode proves that the commit point was crossed.
                        if not _same_regular_file(target_path, ready):
                            raise
                        logger.warning("archive_metadata_published_with_staging_link")
                # No fallible callback after the commit point: success must remain success.
                return True
            finally:
                try:
                    if _same_regular_file(stage, stage_identity):
                        stage.unlink()
                except OSError as exc:
                    logger.warning("archive_metadata_stage_cleanup_failed", errno=exc.errno)


def _same_regular_file(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )


def _metadata_member(entry: zipfile.ZipInfo) -> bool:
    return PurePosixPath(entry.filename.replace("\\", "/")).name.casefold() in _METADATA_NAMES


def _validate_members(entries: list[zipfile.ZipInfo], limit: int, block_dangerous: bool) -> None:
    seen: set[str] = set()
    total = 0
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        mode = stat.S_IFMT(entry.external_attr >> 16)
        expected_mode = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
        if (
            has_archive_member_path_traversal(entry.orig_filename)
            or "\x00" in entry.orig_filename
            or mode not in {0, expected_mode}
            or entry.flag_bits & 1
            or entry.file_size < 0
            or entry.compress_size < 0
            or (entry.is_dir() and entry.file_size != 0)
            or (block_dangerous and PurePosixPath(name).suffix.casefold() in DANGEROUS_EXTENSIONS)
        ):
            raise FileSafetyError("Archive contains an unsafe or unsupported member")
        key = name.casefold()
        if key in seen:
            raise FileSafetyError("Archive contains duplicate member paths")
        seen.add(key)
        total += entry.file_size
        if total > limit:
            raise FileSafetyError("Archive decompressed size exceeds limit")


def _create_stage(target: Path, requested: Path | None) -> tuple[Path, int]:
    if requested is not None:
        return requested, os.open(requested, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fd, name = tempfile.mkstemp(prefix=".pullbox-metadata-", suffix=".cbz", dir=target.parent)
    return Path(name), fd


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _check_source(path: Path, stream: BinaryIO | None, before: os.stat_result) -> None:
    try:
        unchanged = _fingerprint(before) == _fingerprint(path.lstat())
        if stream is not None:
            unchanged = unchanged and _fingerprint(before) == _fingerprint(
                os.fstat(stream.fileno())
            )
    except FileNotFoundError:
        unchanged = False
    if not unchanged:
        raise FileSafetyError("Source archive changed during metadata writing")


def _check_cancelled(check: Callable[[], None] | None) -> None:
    if check is not None:
        check()


def _progress(
    callback: ArchiveMetadataProgress | None,
    check: Callable[[], None] | None,
    stage: str,
    current: int,
    total: int,
    unit: str = "bytes",
) -> None:
    _check_cancelled(check)
    if callback is not None:
        callback(stage, current, total, unit)
    _check_cancelled(check)


def _construct(
    source: MetadataArchiveSource,
    stream: BinaryIO,
    members: list[zipfile.ZipInfo],
    metadata: dict[str, bytes],
    total: int,
    progress: ArchiveMetadataProgress | None,
    check: Callable[[], None] | None,
) -> list[tuple[str, int, int]]:
    current = 0
    _progress(progress, check, "transferring", 0, total)
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as target:
        target.comment = source.comment
        for entry in members:
            _check_cancelled(check)
            with (
                source.open(entry) as incoming,
                target.open(copy(entry), "w", force_zip64=True) as outgoing,
            ):
                while chunk := incoming.read(_CHUNK_BYTES):
                    _check_cancelled(check)
                    outgoing.write(chunk)
                    current += len(chunk)
                    if current > total:
                        raise FileSafetyError("Archive expanded beyond its declared size")
                    _progress(progress, check, "transferring", current, total)
        for name, payload in metadata.items():
            _check_cancelled(check)
            target.writestr(name, payload)
            current += len(payload)
            _progress(progress, check, "transferring", current, total)
        if current != total:
            raise FileSafetyError("Archive payload size disagrees with its directory")
        manifest = [(entry.filename, entry.file_size, entry.CRC) for entry in target.infolist()]
    return manifest


def _verify(
    path: Path,
    manifest: list[tuple[str, int, int]],
    comment: bytes,
    total: int,
    progress: ArchiveMetadataProgress | None,
    check: Callable[[], None] | None,
) -> None:
    _progress(progress, check, "verifying", 0, total)
    with zipfile.ZipFile(path) as archive:
        if archive.comment != comment or manifest != [
            (entry.filename, entry.file_size, entry.CRC) for entry in archive.infolist()
        ]:
            raise FileSafetyError("Temporary archive failed verification")
        current = 0
        for entry in archive.infolist():
            _check_cancelled(check)
            with archive.open(entry) as stream:
                while chunk := stream.read(_CHUNK_BYTES):
                    current += len(chunk)
                    _progress(progress, check, "verifying", current, total)
        if current != total:
            raise FileSafetyError("Temporary archive failed verification")
