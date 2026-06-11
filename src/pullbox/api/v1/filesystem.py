"""Filesystem browsing API — directory and file listing for picker UIs."""

import platform
from collections.abc import Sequence
from pathlib import Path

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.core.file_safety import get_allowed_extensions

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/filesystem", tags=["filesystem"], include_in_schema=False)

# Directories that should never be browsable via the API.
# We resolve() each prefix so symlinks (e.g. macOS /etc → /private/etc) are caught.
_BLOCKED_DIRS: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/boot",
    "/root",
    "/var/log",
    "/var/run",
)
_BLOCKED_PREFIXES: frozenset[str] = frozenset(
    {p for d in _BLOCKED_DIRS for p in (d, str(Path(d).resolve()))}
)

_MAX_PATH_LENGTH = 4096


class DirectoryEntry(BaseModel):
    """A single directory entry returned by the browser."""

    name: str
    path: str


class QuickLink(BaseModel):
    """A shortcut to a common filesystem location (volumes, mounts, home, etc.)."""

    label: str
    path: str
    icon: str  # Hint for the UI: "home", "drive", "network", "folder"


class FileEntry(BaseModel):
    """A single file entry returned by the file browser."""

    name: str
    path: str
    size: int


class DirectoryListing(BaseModel):
    """Response from the directory browse endpoint."""

    parent: str | None
    path: str
    directories: list[DirectoryEntry]
    quick_links: list[QuickLink]


class FileListing(BaseModel):
    """Response from the file browse endpoint (directories + files)."""

    parent: str | None
    path: str
    directories: list[DirectoryEntry]
    files: list[FileEntry]
    quick_links: list[QuickLink]


def _is_within_allowed_roots(path: Path, allowed_roots: Sequence[Path]) -> bool:
    """Return True when ``path`` is inside one of the allowed roots."""
    if not allowed_roots:
        return True
    return any(path == root or root in path.parents for root in allowed_roots)


def _parse_allowed_roots(raw_roots: str | None) -> list[Path]:
    """Parse and sanitize a comma-separated allowed-roots list."""
    if not raw_roots:
        return []

    allowed: list[Path] = []
    for raw_root in raw_roots.split(","):
        candidate = raw_root.strip()
        if not candidate:
            continue
        resolved = _validate_browsable_path(candidate)
        if resolved == Path("/").resolve() and candidate != "/":
            continue
        if resolved not in allowed:
            allowed.append(resolved)
    return allowed


def _constraint_was_requested(raw_roots: str | None) -> bool:
    """Return True when the caller explicitly requested constrained browsing."""
    return raw_roots is not None


def _empty_directory_listing(path: str = "/") -> DirectoryListing:
    """Return a fail-closed empty directory listing for invalid constrained roots."""
    return DirectoryListing(parent=None, path=path, directories=[], quick_links=[])


def _empty_file_listing(path: str = "/") -> FileListing:
    """Return a fail-closed empty file listing for invalid constrained roots."""
    return FileListing(parent=None, path=path, directories=[], files=[], quick_links=[])


def _clamp_parent_to_allowed_roots(
    parent: Path | None,
    allowed_roots: Sequence[Path],
) -> str | None:
    """Return a parent string only when it remains inside the allowed roots."""
    if parent is None:
        return None
    if allowed_roots and not _is_within_allowed_roots(parent, allowed_roots):
        return None
    return str(parent)


def _build_quick_links(allowed_roots: Sequence[Path]) -> list[QuickLink]:
    """Return either constrained root quick links or the default system links."""
    if not allowed_roots:
        return _discover_quick_links()

    links: list[QuickLink] = []
    for root in allowed_roots:
        label = root.name or str(root)
        links.append(QuickLink(label=label, path=str(root), icon="folder"))
    return links


def _validate_browsable_path(path: str, allowed_roots: Sequence[Path] | None = None) -> Path:
    """Validate and resolve a user-supplied path for directory browsing.

    Blocks system directories, path traversal, null bytes, non-printable
    characters, and excessively long paths. Returns Path("/") as a safe
    fallback when the input is rejected.
    """
    roots = list(allowed_roots or [])
    fallback = roots[0] if roots else Path("/").resolve()

    # Strip null bytes
    sanitized = path.replace("\x00", "")

    # Reject non-printable characters
    if not sanitized.isprintable():
        logger.warning("filesystem_path_blocked", requested_path=path, reason="non-printable")
        return fallback

    # Reject overly long paths
    if len(sanitized) > _MAX_PATH_LENGTH:
        logger.warning("filesystem_path_blocked", requested_path=path[:100], reason="too_long")
        return fallback

    # Authenticated operator browser: the raw value is length/character checked,
    # blocked-prefix checked below, and optionally clamped to explicit roots
    # before any listing is returned.
    # codeql[py/path-injection]
    resolved = Path(sanitized).resolve()
    resolved_str = str(resolved)

    # Defense in depth: reject if ".." survived resolution
    if ".." in resolved.parts:
        logger.warning("filesystem_path_blocked", requested_path=path, reason="path_traversal")
        return fallback

    # Block sensitive system directories
    for prefix in _BLOCKED_PREFIXES:
        if resolved_str == prefix or resolved_str.startswith(f"{prefix}/"):
            logger.warning(
                "filesystem_path_blocked", requested_path=path, reason="sensitive_directory"
            )
            return fallback

    # Fallback if path doesn't exist
    # ``resolved`` has passed the browser safety checks above; this probe only
    # decides whether to fall back to a safe root instead of returning content.
    # codeql[py/path-injection]
    if not resolved.exists() or not resolved.is_dir():
        return fallback

    if roots and not _is_within_allowed_roots(resolved, roots):
        logger.warning(
            "filesystem_path_blocked",
            requested_path=path,
            reason="outside_allowed_roots",
        )
        return fallback

    return resolved


def _discover_quick_links() -> list[QuickLink]:
    """Detect common mount points and volumes based on what actually exists.

    OS-agnostic: checks known paths and only returns those that are present
    and readable on this system.
    """
    links: list[QuickLink] = []
    system = platform.system()

    # Home directory — always available
    home = Path.home()
    if home.is_dir():
        links.append(QuickLink(label="Home", path=str(home), icon="home"))

    # Root
    links.append(QuickLink(label="/", path="/", icon="drive"))

    if system == "Darwin":
        # macOS: network shares and external drives mount under /Volumes
        volumes = Path("/Volumes")
        if volumes.is_dir():
            links.append(QuickLink(label="Volumes", path="/Volumes", icon="network"))
            # Also surface individual mounted volumes (skip the boot volume)
            try:
                boot_volume = Path("/").resolve()
                for vol in sorted(volumes.iterdir()):
                    if vol.is_dir() and vol.resolve() != boot_volume:
                        links.append(QuickLink(label=vol.name, path=str(vol), icon="network"))
            except PermissionError:
                pass

    elif system == "Linux":
        # Linux: common mount locations
        for mount_dir, label in [
            ("/mnt", "Mounts"),
            ("/media", "Media"),
        ]:
            p = Path(mount_dir)
            if p.is_dir():
                links.append(QuickLink(label=label, path=mount_dir, icon="network"))
                # Surface immediate children (mounted shares/drives)
                try:
                    for child in sorted(p.iterdir()):
                        if child.is_dir() and not child.name.startswith("."):
                            links.append(
                                QuickLink(label=child.name, path=str(child), icon="network")
                            )
                except PermissionError:
                    pass

    elif system == "Windows":
        # Windows: drive letters
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                links.append(QuickLink(label=f"{letter}:", path=str(drive), icon="drive"))

    return links


@router.get("/directories", response_model=DirectoryListing)
async def list_directories(
    _user: InteractiveOperatorUser,
    path: str = Query("/", description="Directory path to list"),
    roots: str | None = Query(
        None,
        description="Optional comma-separated allowed root directories for constrained browsing",
    ),
) -> DirectoryListing:
    """List subdirectories of a given path for the folder browser UI.

    Requires authentication. Only returns directories (not files) and
    skips hidden entries. Includes quick-link shortcuts to common mount
    points and volumes.
    """
    allowed_roots = _parse_allowed_roots(roots)
    if _constraint_was_requested(roots) and not allowed_roots:
        logger.warning("filesystem_browse_rejected_all_roots", requested_roots=roots)
        return _empty_directory_listing()
    target = _validate_browsable_path(path, allowed_roots)

    parent_path = target.parent if target != target.parent else None
    parent = _clamp_parent_to_allowed_roots(parent_path, allowed_roots)

    directories: list[DirectoryEntry] = []
    try:
        for entry in sorted(target.iterdir()):
            # Skip hidden directories and non-directories
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            directories.append(DirectoryEntry(name=entry.name, path=str(entry)))
    except PermissionError:
        logger.warning("directory_browse_permission_denied", path=str(target))

    return DirectoryListing(
        parent=parent,
        path=str(target),
        directories=directories,
        quick_links=_build_quick_links(allowed_roots),
    )


@router.get("/browse", response_model=FileListing)
async def browse_files(
    _user: InteractiveOperatorUser,
    session: DbSession,
    path: str = Query("/", description="Directory path to list"),
    roots: str | None = Query(
        None,
        description="Optional comma-separated allowed root directories for constrained browsing",
    ),
    extensions: str | None = Query(
        None,
        description="Comma-separated file extensions to show (e.g. 'cbz,cbr,pdf')",
    ),
) -> FileListing:
    """List directories and files for the file picker UI.

    Returns subdirectories and files (optionally filtered by extension).
    Defaults to comic file extensions when no filter is provided.
    """
    allowed_roots = _parse_allowed_roots(roots)
    if _constraint_was_requested(roots) and not allowed_roots:
        logger.warning("filesystem_browse_rejected_all_roots", requested_roots=roots)
        return _empty_file_listing()
    target = _validate_browsable_path(path, allowed_roots)
    parent_path = target.parent if target != target.parent else None
    parent = _clamp_parent_to_allowed_roots(parent_path, allowed_roots)

    # Build extension filter
    if extensions:
        ext_filter = frozenset(
            f".{e.strip().lower().lstrip('.')}" for e in extensions.split(",") if e.strip()
        )
    else:
        ext_filter = frozenset(await get_allowed_extensions(session))

    directories: list[DirectoryEntry] = []
    files: list[FileEntry] = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                directories.append(DirectoryEntry(name=entry.name, path=str(entry)))
            elif entry.is_file() and entry.suffix.lower() in ext_filter:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                files.append(FileEntry(name=entry.name, path=str(entry), size=size))
    except PermissionError:
        logger.warning("file_browse_permission_denied", path=str(target))

    return FileListing(
        parent=parent,
        path=str(target),
        directories=directories,
        files=files,
        quick_links=_build_quick_links(allowed_roots),
    )
