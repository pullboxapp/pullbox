"""Issue library-file lifecycle helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.config import get_settings
from pullbox.core.config_resolver import load_system_config_values
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.services.series_delete_targets import trash_relative_path
from pullbox.utilities.settings import move_file_to_utility_trash, resolve_trash_directory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.series import Series


@dataclass(frozen=True, slots=True)
class IssueFileDeleteResult:
    """Result of removing the library file linked to an issue."""

    issue_id: int
    status: IssueStatus
    file_deleted: bool
    trashed: bool
    trash_path: Path | None = None


async def resolve_configured_utility_trash_dir(session: AsyncSession) -> Path | None:
    """Resolve the optional utility trash directory from editable app settings."""
    configs = await load_system_config_values(session, ("utility_trash_folder",))
    settings = get_settings()
    return resolve_trash_directory(
        configs.get("utility_trash_folder", ""),
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )


async def delete_issue_library_file(
    session: AsyncSession,
    issue_id: int,
) -> IssueFileDeleteResult:
    """Delete or trash the file linked to an issue and make the issue importable again."""
    result = await session.execute(
        select(Issue)
        .options(
            joinedload(Issue.series),
            joinedload(Issue.library_file).joinedload(LibraryFile.library_root),
        )
        .where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)
    if issue.library_file is None:
        raise ValidationError("Issue does not have a linked library file.")

    library_file = issue.library_file
    file_path = Path(library_file.file_path)
    trash_dir = await resolve_configured_utility_trash_dir(session)
    trash_path: Path | None = None
    file_deleted = False
    trashed = False

    if (
        library_file.storage_mode is not LibraryFileStorageMode.REFERENCED
        and await asyncio.to_thread(file_path.exists)
    ):
        if trash_dir is not None:
            trash_path = await asyncio.to_thread(
                move_file_to_utility_trash,
                file_path,
                trash_dir,
                relative_path=trash_relative_path(file_path, library_file.library_root),
            )
            trashed = True
            file_deleted = True
        else:
            await asyncio.to_thread(file_path.unlink)
            file_deleted = True

    await session.delete(library_file)
    issue.status = IssueStatus.WANTED if _series_is_monitored(issue.series) else IssueStatus.SKIPPED
    issue.manual_skip = False
    await session.flush()

    return IssueFileDeleteResult(
        issue_id=issue.id,
        status=issue.status,
        file_deleted=file_deleted,
        trashed=trashed,
        trash_path=trash_path,
    )


def _series_is_monitored(series: Series | None) -> bool:
    return bool(series is not None and series.monitored)
