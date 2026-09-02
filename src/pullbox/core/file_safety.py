"""File safety — allowlists, dangerous file detection, and archive protection.

Provides centralized security checks used during:
- Post-processing of completed downloads (download_task.py)
- Library scanning (library_service.py)

All settings are read from SystemConfig and can be managed via the
Security → File Safety page in the UI.
"""

from __future__ import annotations

import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISLNK
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.core.comicinfo import ComicInfoData, parse_comicinfo
from pullbox.core.filesystem_scan import iter_supported_files_with_handler
from pullbox.core.page_sources.base import canonical_page_names

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ── Defaults ──────────────────────────────────────────────────

DEFAULT_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".cbr", ".cbz", ".cb7", ".cbt", ".pdf", ".epub"}
)

DANGEROUS_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Windows executables & installers
        ".exe",
        ".bat",
        ".cmd",
        ".com",
        ".msi",
        ".msp",
        ".mst",
        # PowerShell & Windows scripting
        ".ps1",
        ".psm1",
        ".psd1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsh",
        ".wsf",
        # System files
        ".scr",
        ".cpl",
        ".dll",
        ".sys",
        ".drv",
        # Unix/Linux scripts
        ".sh",
        ".bash",
        ".csh",
        ".ksh",
        ".zsh",
        # macOS
        ".app",
        ".action",
        ".command",
        ".workflow",
        # Other dangerous
        ".reg",
        ".inf",
        ".hta",
        ".pif",
        ".lnk",
    }
)


class FileSafetyError(Exception):
    """Raised when a file safety check fails."""

    def __init__(self, reason: str, details: list[str] | None = None) -> None:
        self.reason = reason
        self.details = details or []
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ResourceSafetyBlock:
    """A user-reviewable resource safety stop.

    These are intentionally narrower than all file-safety failures. Users may
    approve resource limit exceptions once, but corrupt archives, traversal
    attacks, and dangerous payloads remain hard failures.
    """

    kind: str
    reason: str
    details: list[str]
    source: str = "runtime"
    overrideable: bool = True

    def to_diagnostics(self) -> dict[str, Any]:
        """Return the persisted diagnostics payload used by import review rows."""
        return {
            "kind": self.kind,
            "reason": self.reason,
            "details": list(self.details),
            "source": self.source,
            "overrideable": self.overrideable,
        }


@dataclass(frozen=True, slots=True)
class ZipArchiveSafetyReport:
    """Single-pass safety facts for one ZIP-based archive."""

    archive_path: Path
    total_size: int
    traversal_entries: list[str]
    dangerous_entries: list[str]
    entry_names: tuple[str, ...]
    comicinfo: ComicInfoData | None
    comicinfo_entry: str | None
    comicinfo_entry_count: int
    comicinfo_error: str | None
    page_count: int | None = None


@dataclass(frozen=True, slots=True)
class FileSafetyInspection:
    """Immutable transient evidence returned for a single-file safety check."""

    archives: tuple[ZipArchiveSafetyReport, ...] = ()


_MAX_COMICINFO_XML_BYTES = 2 * 1024 * 1024


_ARCHIVE_SIZE_MARKERS = (
    "archive decompressed size",
    "exceeds limit",
)
_PILLOW_RESOURCE_MARKERS = (
    "decompressionbomberror",
    "decompression bomb",
    "image size exceeds limit",
    "safe rasterization limits",
)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def classify_resource_safety_exception(exc: BaseException) -> ResourceSafetyBlock | None:
    """Return a reviewable resource safety block for overrideable failures only."""
    for candidate in _exception_chain(exc):
        if isinstance(candidate, FileSafetyError):
            normalized_reason = candidate.reason.lower()
            if all(marker in normalized_reason for marker in _ARCHIVE_SIZE_MARKERS):
                return ResourceSafetyBlock(
                    kind="archive_decompressed_size",
                    reason=candidate.reason,
                    details=list(candidate.details),
                    source="file_safety",
                )
            continue

        exc_type = candidate.__class__.__name__.lower()
        message = str(candidate).lower()
        if exc_type == "decompressionbomberror" or any(
            marker in message for marker in _PILLOW_RESOURCE_MARKERS
        ):
            return ResourceSafetyBlock(
                kind="pillow_decompression_bomb",
                reason=(
                    "File exceeded Pullbox's safe image processing limit. "
                    "Allow once only if you trust this source and have enough memory."
                ),
                details=[str(candidate)],
                source="image_processing",
            )
    return None


def is_resource_safety_exception_allowed(diagnostics: Mapping[str, Any] | None) -> bool:
    """Return true when a persisted one-time safety exception has been approved."""
    if not isinstance(diagnostics, Mapping):
        return False
    safety_exception = diagnostics.get("safety_exception")
    if not isinstance(safety_exception, Mapping):
        return False
    if safety_exception.get("allowed_once") is not True:
        return False
    previous_block = safety_exception.get("previous_block")
    if not isinstance(previous_block, Mapping):
        return False
    if previous_block.get("code") in {"archive_no_pages", "single_page_comic"}:
        return False
    return bool(previous_block.get("overrideable", True))


# ── Config Loading ────────────────────────────────────────────


async def get_allowed_extensions(session: AsyncSession) -> set[str]:
    """Load the import allowlist from SystemConfig.

    Returns the configured set of extensions, or the defaults if not configured.
    Extensions are normalised to lowercase with a leading dot.
    """

    from pullbox.models.config import SystemConfig

    row = await session.get(SystemConfig, "allowed_import_extensions")
    if row and row.value:
        raw = row.value.split(",")
        exts = set()
        for ext in raw:
            ext = ext.strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                exts.add(ext)
        return exts

    return set(DEFAULT_ALLOWED_EXTENSIONS)


async def is_dangerous_file_blocking_enabled(session: AsyncSession) -> bool:
    """Check whether dangerous file detection is enabled (default: True)."""
    from pullbox.models.config import SystemConfig

    row = await session.get(SystemConfig, "block_dangerous_files")
    if row and row.value:
        return row.value.lower() != "false"
    return True  # Default: enabled


async def get_archive_size_limit_bytes(session: AsyncSession) -> int:
    """Load the archive bomb size limit from SystemConfig (default: 2000 MB)."""
    from pullbox.models.config import SystemConfig

    row = await session.get(SystemConfig, "archive_size_limit_mb")
    if row and row.value:
        try:
            return int(row.value) * 1024 * 1024
        except ValueError:
            pass
    return 2000 * 1024 * 1024  # 2 GB


# ── Directory Scanning ────────────────────────────────────────


def scan_directory_for_dangerous_files(directory: Path) -> list[Path]:
    """Walk a directory tree and return all files with dangerous extensions.

    This checks the raw files on disk — not inside archives.
    """
    dangerous: list[Path] = []

    if directory.is_file():
        if directory.suffix.lower() in DANGEROUS_EXTENSIONS:
            dangerous.append(directory)
        return dangerous

    if not directory.is_dir():
        return dangerous

    def _on_scan_error(root: Path, exc: OSError) -> None:
        # NotADirectoryError can occur on network mounts (NFS/SMB) when
        # recursive directory walkers descend into a file that the OS falsely
        # reports as a directory due to stale metadata caching.
        logger.warning("file_safety_scan_error", path=str(root), error=str(exc))

    dangerous.extend(
        iter_supported_files_with_handler(directory, DANGEROUS_EXTENSIONS, _on_scan_error)
    )

    return dangerous


# ── Archive Safety Checks ─────────────────────────────────────


def has_archive_member_path_traversal(entry_name: str) -> bool:
    """Check if an archive entry name contains path traversal sequences."""
    # Check both POSIX and Windows path separators
    for path_cls in (PurePosixPath, PureWindowsPath):
        parts = path_cls(entry_name).parts
        if ".." in parts:
            return True

    # Check for absolute paths
    if entry_name.startswith("/") or entry_name.startswith("\\"):
        return True
    # Windows drive letters
    return len(entry_name) >= 2 and entry_name[1] == ":"


def _has_path_traversal(entry_name: str) -> bool:
    """Backward-compatible alias for archive member path traversal checks."""
    return has_archive_member_path_traversal(entry_name)


def ensure_zip_archive_inspectable(archive_path: Path) -> None:
    """Fail closed when a ZIP-based archive cannot be inspected."""
    if archive_path.suffix.lower() not in (".cbz", ".zip"):
        return

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(
            "archive_inspection_failed",
            path=str(archive_path),
            error=str(exc),
        )
        raise FileSafetyError(
            f"Archive could not be inspected: {archive_path.name}",
            details=[str(archive_path)],
        ) from exc


def check_archive_path_traversal(archive_path: Path) -> list[str]:
    """Check archive entries for path traversal attacks.

    Returns a list of offending entry names (empty = safe).
    Only checks ZIP-based archives (.cbz) since those are the most common
    and we have stdlib support. RAR/7z support can be added later.
    """
    offending: list[str] = []
    ext = archive_path.suffix.lower()

    if ext in (".cbz", ".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for entry in zf.namelist():
                    if _has_path_traversal(entry):
                        offending.append(entry)
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning(
                "archive_traversal_check_failed",
                path=str(archive_path),
                error=str(exc),
            )

    return offending


def check_archive_size(archive_path: Path, max_bytes: int) -> int | None:
    """Check the total decompressed size of a ZIP-based archive.

    Returns the total uncompressed size in bytes, or None if the archive
    cannot be read. Raises FileSafetyError if the size exceeds the limit.
    """
    ext = archive_path.suffix.lower()

    if ext not in (".cbz", ".zip"):
        return None

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            total_size = sum(info.file_size for info in zf.infolist())

        if total_size > max_bytes:
            raise FileSafetyError(
                f"Archive decompressed size ({total_size:,} bytes) exceeds "
                f"limit ({max_bytes:,} bytes)",
                details=[str(archive_path)],
            )
        return total_size

    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(
            "archive_size_check_failed",
            path=str(archive_path),
            error=str(exc),
        )
        return None


def check_archive_contents_for_dangerous_files(archive_path: Path) -> list[str]:
    """Check inside a ZIP-based archive for files with dangerous extensions.

    Returns a list of dangerous entry names (empty = safe).
    """
    dangerous: list[str] = []
    ext = archive_path.suffix.lower()

    if ext in (".cbz", ".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for entry in zf.namelist():
                    entry_ext = Path(entry).suffix.lower()
                    if entry_ext in DANGEROUS_EXTENSIONS:
                        dangerous.append(entry)
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning(
                "archive_dangerous_check_failed",
                path=str(archive_path),
                error=str(exc),
            )

    return dangerous


def inspect_zip_archive_safety(
    archive_path: Path,
    *,
    block_dangerous: bool,
) -> ZipArchiveSafetyReport | None:
    """Inspect a ZIP-based archive once and return all safety facts needed."""
    if archive_path.suffix.lower() not in (".cbz", ".zip"):
        return None

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            entries = zf.infolist()

            total_size = 0
            traversal_entries: list[str] = []
            dangerous_entries: list[str] = []
            for entry in entries:
                entry_name = entry.filename
                total_size += entry.file_size
                if _has_path_traversal(entry_name):
                    traversal_entries.append(entry_name)
                if block_dangerous and Path(entry_name).suffix.lower() in DANGEROUS_EXTENSIONS:
                    dangerous_entries.append(entry_name)

            comicinfo_entries = sorted(
                (
                    entry
                    for entry in entries
                    if not entry.is_dir()
                    and PurePosixPath(entry.filename.replace("\\", "/")).name.lower()
                    == "comicinfo.xml"
                ),
                key=lambda entry: entry.filename.casefold(),
            )
            comicinfo: ComicInfoData | None = None
            comicinfo_entry = comicinfo_entries[0].filename if comicinfo_entries else None
            comicinfo_error: str | None = None
            if comicinfo_entries:
                comicinfo_member = comicinfo_entries[0]
                if comicinfo_member.file_size > _MAX_COMICINFO_XML_BYTES:
                    comicinfo_error = "comicinfo_size_limit"
                else:
                    try:
                        with zf.open(comicinfo_member, "r") as member:
                            xml_bytes = member.read(_MAX_COMICINFO_XML_BYTES + 1)
                        if len(xml_bytes) > _MAX_COMICINFO_XML_BYTES:
                            comicinfo_error = "comicinfo_size_limit"
                        else:
                            comicinfo = parse_comicinfo(xml_bytes.decode("utf-8", errors="replace"))
                    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                        logger.warning(
                            "archive_comicinfo_inspection_failed",
                            path=str(archive_path),
                            error=str(exc),
                        )
                        comicinfo_error = "comicinfo_unreadable"
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(
            "archive_inspection_failed",
            path=str(archive_path),
            error=str(exc),
        )
        raise FileSafetyError(
            f"Archive could not be inspected: {archive_path.name}",
            details=[str(archive_path)],
        ) from exc

    return ZipArchiveSafetyReport(
        archive_path=archive_path,
        total_size=total_size,
        traversal_entries=traversal_entries,
        dangerous_entries=dangerous_entries,
        entry_names=tuple(entry.filename for entry in entries),
        comicinfo=comicinfo,
        comicinfo_entry=comicinfo_entry,
        comicinfo_entry_count=len(comicinfo_entries),
        comicinfo_error=comicinfo_error,
        page_count=len(
            canonical_page_names(
                [
                    entry.filename
                    for entry in entries
                    if not entry.is_dir()
                    and entry.file_size > 0
                    and not S_ISLNK(entry.external_attr >> 16)
                ]
            )
        ),
    )


# ── High-Level Enforcement ────────────────────────────────────


def run_safety_checks(
    download_path: Path,
    *,
    block_dangerous: bool,
    max_archive_size: int,
) -> FileSafetyInspection:
    """Run all file safety checks synchronously.

    This is a pure-sync function that performs filesystem I/O only (no
    database access).  Call it from a thread-pool executor so it doesn't
    block the async event loop — especially important when the download
    path is on a network mount (NFS/SMB).

    Raises ``FileSafetyError`` if any check fails.
    """
    log = logger.bind(download_path=str(download_path))

    # 1. Dangerous files on disk
    if block_dangerous:
        dangerous_files = scan_directory_for_dangerous_files(download_path)
        if dangerous_files:
            names = [
                str(f.relative_to(download_path) if f.is_relative_to(download_path) else f)
                for f in dangerous_files
            ]
            log.warning(
                "file_safety_dangerous_files_detected",
                count=len(dangerous_files),
                files=names,
            )
            raise FileSafetyError(
                f"Download contains {len(dangerous_files)} dangerous file(s) — "
                f"entire release rejected",
                details=names,
            )

    # Find archive files to inspect
    archive_files: list[Path] = []
    if download_path.is_file():
        if download_path.suffix.lower() in (".cbz", ".zip"):
            archive_files.append(download_path)
    elif download_path.is_dir():

        def _on_archive_scan_error(root: Path, exc: OSError) -> None:
            log.warning(
                "file_safety_archive_scan_error",
                path=str(root),
                error=str(exc),
            )

        archive_files.extend(
            iter_supported_files_with_handler(
                download_path,
                frozenset({".cbz", ".zip"}),
                _on_archive_scan_error,
            )
        )

    inspected_archives: list[ZipArchiveSafetyReport] = []
    collect_evidence = download_path.is_file()
    for archive in archive_files:
        safety_report = inspect_zip_archive_safety(
            archive,
            block_dangerous=block_dangerous,
        )
        if safety_report is None:
            continue

        # 2. Path traversal
        if safety_report.traversal_entries:
            log.warning(
                "file_safety_path_traversal_detected",
                archive=str(archive),
                entries=safety_report.traversal_entries,
            )
            raise FileSafetyError(
                "Archive contains path traversal entries — entire release rejected",
                details=safety_report.traversal_entries,
            )

        # 3. Archive bomb
        if safety_report.total_size > max_archive_size:
            raise FileSafetyError(
                f"Archive decompressed size ({safety_report.total_size:,} bytes) exceeds "
                f"limit ({max_archive_size:,} bytes)",
                details=[str(archive)],
            )

        # 4. Dangerous files inside archives
        if safety_report.dangerous_entries:
            log.warning(
                "file_safety_dangerous_inside_archive",
                archive=str(archive),
                entries=safety_report.dangerous_entries,
            )
            raise FileSafetyError(
                f"Archive contains {len(safety_report.dangerous_entries)} dangerous file(s) — "
                f"entire release rejected",
                details=safety_report.dangerous_entries,
            )

        if collect_evidence:
            inspected_archives.append(safety_report)

    log.debug("file_safety_checks_passed")
    return FileSafetyInspection(archives=tuple(inspected_archives))


async def check_download_safety(
    session: AsyncSession,
    download_path: Path,
) -> None:
    """Run all file safety checks on a completed download directory.

    Pre-fetches config from the database (async), then delegates to
    :func:`run_safety_checks` which can be run in a thread-pool executor
    by the caller to avoid blocking the event loop during slow NFS I/O.

    Raises ``FileSafetyError`` if any check fails.
    """
    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_size = await get_archive_size_limit_bytes(session)
    run_safety_checks(download_path, block_dangerous=block_dangerous, max_archive_size=max_size)
