"""Leave-in-place file handling for library registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_root_resolution import path_is_inside_root

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryRoot


async def handle_leave_in_place(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    ingest_policy: LibraryIngestPolicy,
    rename: bool,
) -> Path:
    """Return a contained source path without ever mutating a referenced file."""
    _ = session, issue, series, ingest_policy
    if not root.enabled or not path_is_inside_root(source_path, root):
        raise ConfigurationError("Referenced files must be inside an enabled library root.")
    if rename:
        raise ConfigurationError("Referenced library files cannot rename source files.")
    return source_path.resolve(strict=True)
