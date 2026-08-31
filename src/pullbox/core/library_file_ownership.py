"""Ownership and path validation for managed and referenced library files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from pullbox.core.exceptions import ConfigurationError, ValidationError
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SIGNATURE_KEYS = (
    "schema_version",
    "resolved_path",
    "size",
    "mtime_ns",
    "device",
    "inode",
)


class ReferencedFileValidationError(ConfigurationError):
    """Referenced-file validation failure with a stable review reason."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class ReferencedFileMutationError(ValidationError):
    """Raised when an operation would mutate a user-owned referenced artifact."""


async def referenced_library_files_for_target(
    session: AsyncSession,
    target: Path,
    *,
    include_descendants: bool,
) -> list[LibraryFile]:
    """Return referenced records at a file path or below a folder path."""
    target_paths = {
        str(target.expanduser().absolute()).rstrip("/"),
        str(target.expanduser().resolve(strict=False)).rstrip("/"),
    }
    query = select(LibraryFile).where(LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED)
    if include_descendants:
        conditions: list[ColumnElement[bool]] = []
        for target_path in target_paths:
            conditions.extend(
                (
                    LibraryFile.file_path == target_path,
                    LibraryFile.file_path.startswith(f"{target_path}/", autoescape=True),
                )
            )
        query = query.where(or_(*conditions))
    else:
        query = query.where(LibraryFile.file_path.in_(target_paths))
    return list((await session.execute(query)).scalars().all())


async def require_mutable_library_target(
    session: AsyncSession,
    target: Path,
    *,
    include_descendants: bool,
    operation: str,
) -> None:
    """Reject mutations that include any referenced library file."""
    referenced_files = await referenced_library_files_for_target(
        session,
        target,
        include_descendants=include_descendants,
    )
    if referenced_files:
        raise ReferencedFileMutationError(
            f"Referenced library files cannot be {operation}. They must stay unchanged on disk."
        )


def build_file_identity_signature(path: Path) -> dict[str, int | str]:
    """Capture the portable scan/execution identity used by library files."""
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ConfigurationError("Library path must be an existing file.")
    stat_result = resolved.stat()
    return {
        "schema_version": 1,
        "resolved_path": str(resolved),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
    }


def validate_file_identity_signature(
    expected: dict[str, object],
    current: dict[str, int | str],
) -> None:
    """Fail closed when a referenced file no longer matches its scan evidence."""
    if not expected or any(key not in expected for key in _SIGNATURE_KEYS):
        raise ReferencedFileValidationError(
            "source_signature_missing",
            "Referenced file is missing the scan evidence required for in-place import.",
        )
    if expected.get("schema_version") != 1:
        raise ReferencedFileValidationError(
            "source_signature_unsupported",
            "Referenced file uses an unsupported scan-evidence version.",
        )
    if any(expected.get(key) != current.get(key) for key in _SIGNATURE_KEYS):
        raise ReferencedFileValidationError(
            "source_changed",
            "Referenced file changed after it was scanned. Rescan before importing it in place.",
        )


async def resolve_referenced_library_root(
    session: AsyncSession,
    source_path: Path,
    explicit_root_id: int | None,
) -> tuple[LibraryRoot, Path, dict[str, int | str]]:
    """Resolve an existing file inside one enabled root without prefix guesses."""
    raw_path = str(source_path)
    if _CONTROL_CHARACTER_RE.search(raw_path) or ".." in source_path.parts:
        raise ConfigurationError("Referenced library path contains unsafe path components.")

    try:
        lexical_source = source_path.expanduser().absolute()
        resolved_source = source_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError("Referenced library path is unavailable.") from exc

    if not resolved_source.is_file():
        raise ConfigurationError("Referenced library path must be an existing file.")
    if not os.access(resolved_source, os.R_OK):
        raise ConfigurationError("Referenced library file is not readable by Pullbox.")

    roots = list(
        (await session.execute(select(LibraryRoot).where(LibraryRoot.enabled.is_(True))))
        .scalars()
        .all()
    )
    if explicit_root_id is not None:
        roots = [root for root in roots if root.id == explicit_root_id]
        if not roots:
            raise ConfigurationError("Selected library root is missing or disabled.")

    candidates: list[tuple[int, LibraryRoot]] = []
    for root in roots:
        try:
            lexical_root = Path(root.path).expanduser().absolute()
            resolved_root = Path(root.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        lexical_inside = lexical_source == lexical_root or lexical_source.is_relative_to(
            lexical_root
        )
        resolved_inside = resolved_source == resolved_root or resolved_source.is_relative_to(
            resolved_root
        )
        if lexical_inside and resolved_inside:
            candidates.append((len(resolved_root.parts), root))

    if not candidates:
        raise ConfigurationError("Referenced files must be inside an enabled library root.")

    _, root = max(candidates, key=lambda candidate: candidate[0])
    return root, resolved_source, build_file_identity_signature(resolved_source)


async def resolve_referenced_source_root(
    session: AsyncSession,
    source_path: Path,
    explicit_root_id: int | None,
) -> tuple[LibraryRoot, Path]:
    """Resolve an import source directory inside one enabled library root."""
    raw_path = str(source_path)
    if _CONTROL_CHARACTER_RE.search(raw_path) or ".." in source_path.parts:
        raise ReferencedFileValidationError(
            "source_path_unsafe",
            "In-place import source contains unsafe path components.",
        )

    try:
        # These probes only normalize the candidate. Both lexical and resolved
        # enabled-root containment are required below before accepting a source.
        # codeql[py/path-injection]
        lexical_source = source_path.expanduser().absolute()
        # codeql[py/path-injection]
        resolved_source = source_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReferencedFileValidationError(
            "source_missing",
            "In-place import source is unavailable.",
        ) from exc

    if not resolved_source.is_dir():
        raise ReferencedFileValidationError(
            "source_missing",
            "In-place import source must be an existing directory.",
        )
    if not os.access(resolved_source, os.R_OK | os.X_OK):
        raise ReferencedFileValidationError(
            "source_unreadable",
            "In-place import source is not readable by Pullbox.",
        )

    roots = list(
        (await session.execute(select(LibraryRoot).where(LibraryRoot.enabled.is_(True))))
        .scalars()
        .all()
    )
    if explicit_root_id is not None:
        roots = [root for root in roots if root.id == explicit_root_id]
        if not roots:
            raise ReferencedFileValidationError(
                "source_outside_root",
                "Selected library root is missing or disabled.",
            )

    candidates: list[tuple[int, LibraryRoot]] = []
    for root in roots:
        try:
            lexical_root = Path(root.path).expanduser().absolute()
            resolved_root = Path(root.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        lexical_inside = lexical_source == lexical_root or lexical_source.is_relative_to(
            lexical_root
        )
        resolved_inside = resolved_source == resolved_root or resolved_source.is_relative_to(
            resolved_root
        )
        if lexical_inside and resolved_inside:
            candidates.append((len(resolved_root.parts), root))

    if not candidates:
        raise ReferencedFileValidationError(
            "source_outside_root",
            "In-place import source must be inside an enabled library root.",
        )

    _, root = max(candidates, key=lambda candidate: candidate[0])
    return root, resolved_source
