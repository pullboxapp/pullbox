"""Ownership and path validation for managed and referenced library files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import ConfigurationError
from pullbox.models.library import LibraryRoot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


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
