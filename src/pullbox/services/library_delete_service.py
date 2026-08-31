"""Library browser delete helpers for file/folder/series cleanup."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from pullbox.core.exceptions import ValidationError
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series
from pullbox.services.series_service import SeriesService
from pullbox.utilities.settings import move_path_to_utility_trash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _path_prefix(path: Path) -> str:
    return str(path).rstrip("/")


def _normalize_series_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    return str(Path(path_value).expanduser().resolve(strict=False)).rstrip("/")


_CV_SUFFIX_RE = re.compile(r"^(?P<base>.+?) \[(?P<comicvine_id>\d+)\]$")


async def _match_series_folder_record(
    session: AsyncSession, target: Path
) -> tuple[int, str] | None:
    """Resolve a folder target back to a series record, even if the stored path is stale."""
    prefix = _path_prefix(target)
    exact_match = (
        await session.execute(select(Series.id, Series.title).where(Series.path == prefix))
    ).one_or_none()
    if exact_match is not None:
        return int(exact_match[0]), str(exact_match[1] or target.name)

    normalized_target = _normalize_series_path(prefix)
    if normalized_target is not None:
        for series in (
            (await session.execute(select(Series).where(Series.path.is_not(None)))).scalars().all()
        ):
            if _normalize_series_path(series.path) == normalized_target:
                return int(series.id), str(series.title or target.name)

    suffix_match = _CV_SUFFIX_RE.match(target.name)
    if suffix_match is None:
        return None

    comicvine_id = int(suffix_match.group("comicvine_id"))
    base_name = suffix_match.group("base")
    series_record = (
        await session.execute(select(Series).where(Series.comicvine_id == comicvine_id))
    ).scalar_one_or_none()
    if series_record is None:
        return None

    stored_name = Path(series_record.path).name if series_record.path else ""
    if stored_name == base_name:
        return int(series_record.id), str(series_record.title or target.name)

    return None


def _status_after_library_file_removed(issue: Issue) -> IssueStatus:
    """Restore an issue to its pre-owned intent once the linked file is removed."""
    if issue.manual_skip:
        return IssueStatus.SKIPPED
    if issue.series is not None and issue.series.monitored:
        return IssueStatus.WANTED
    return IssueStatus.SKIPPED


def _trash_relative_path(source: Path, root: LibraryRoot) -> Path:
    root_path = Path(root.path).expanduser().resolve(strict=False)
    if source == root_path or _is_relative_to(source, root_path):
        return Path(root.name) / source.relative_to(root_path)
    return Path(source.name)


def _delete_path_permanently(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
        return
    if target.is_dir():
        shutil.rmtree(target)
        return
    raise ValidationError("Selected library item no longer exists on disk.")


@dataclass(slots=True)
class LibraryDeleteContext:
    """Delete metadata used by the Library browser UI."""

    mode: str
    trash_enabled: bool
    series_id: int | None = None
    series_title: str | None = None
    linked_file_count: int = 0
    tracked_file_count: int = 0
    tracked_series_count: int = 0
    managed_file_count: int = 0
    referenced_file_count: int = 0
    has_linked_issue: bool = False
    issue_status_after_delete: str | None = None
    issue_status_reason: str | None = None


@dataclass(slots=True)
class LibraryDeleteOutcome:
    """Delete outcome returned to the API layer."""

    kind: str
    mode: str
    source_path: str
    deleted_via_trash: bool
    result_path: str | None = None
    managed_files_deleted: int = 0
    referenced_files_detached: int = 0


async def _storage_mode_counts(
    session: AsyncSession,
    file_clause: ColumnElement[bool],
) -> tuple[int, int]:
    rows = (
        await session.execute(
            select(LibraryFile.storage_mode, func.count(LibraryFile.id))
            .where(file_clause)
            .group_by(LibraryFile.storage_mode)
        )
    ).all()
    counts = {storage_mode: int(count) for storage_mode, count in rows}
    return (
        counts.get(LibraryFileStorageMode.MANAGED, 0),
        counts.get(LibraryFileStorageMode.REFERENCED, 0),
    )


async def build_delete_context(
    session: AsyncSession,
    *,
    target: Path,
    kind: str,
    trash_enabled: bool,
) -> LibraryDeleteContext:
    """Describe how a library target should be deleted."""
    if kind == "root":
        return LibraryDeleteContext(mode="root", trash_enabled=False)

    prefix = _path_prefix(target)

    if kind == "folder":
        series_row = await _match_series_folder_record(session, target)
        if series_row is not None:
            series_id, series_title = int(series_row[0]), str(series_row[1] or target.name)
            linked_file_count = int(
                (
                    await session.execute(
                        select(func.count(LibraryFile.id)).where(
                            LibraryFile.issue_id.in_(
                                select(Issue.id).where(Issue.series_id == series_id)
                            )
                        )
                    )
                ).scalar_one()
            )
            issue_ids = select(Issue.id).where(Issue.series_id == series_id)
            managed_file_count, referenced_file_count = await _storage_mode_counts(
                session,
                LibraryFile.issue_id.in_(issue_ids),
            )
            return LibraryDeleteContext(
                mode="series",
                trash_enabled=trash_enabled,
                series_id=series_id,
                series_title=series_title,
                linked_file_count=linked_file_count,
                managed_file_count=managed_file_count,
                referenced_file_count=referenced_file_count,
            )

    file_clause = (
        LibraryFile.file_path == prefix
        if kind == "file"
        else or_(LibraryFile.file_path == prefix, LibraryFile.file_path.like(f"{prefix}/%"))
    )
    tracked_file_count = int(
        (await session.execute(select(func.count(LibraryFile.id)).where(file_clause))).scalar_one()
    )
    managed_file_count, referenced_file_count = await _storage_mode_counts(session, file_clause)

    tracked_series_count = 0
    if kind == "folder":
        tracked_series_count = int(
            (
                await session.execute(
                    select(func.count(Series.id)).where(
                        or_(Series.path == prefix, Series.path.like(f"{prefix}/%"))
                    )
                )
            ).scalar_one()
        )

    has_linked_issue = False
    issue_status_after_delete: str | None = None
    issue_status_reason: str | None = None
    if kind == "file":
        tracked_file = (
            await session.execute(
                select(LibraryFile)
                .options(selectinload(LibraryFile.issue).selectinload(Issue.series))
                .where(LibraryFile.file_path == prefix)
            )
        ).scalar_one_or_none()
        if tracked_file is not None and tracked_file.issue is not None:
            has_linked_issue = True
            issue_status_after_delete = _status_after_library_file_removed(tracked_file.issue).value
            if tracked_file.issue.manual_skip:
                issue_status_reason = "manual_skip"
            elif tracked_file.issue.series is not None and tracked_file.issue.series.monitored:
                issue_status_reason = "series_monitored"
            else:
                issue_status_reason = "series_unmonitored"

    return LibraryDeleteContext(
        mode="file" if kind == "file" else "folder",
        trash_enabled=trash_enabled,
        tracked_file_count=tracked_file_count,
        tracked_series_count=tracked_series_count,
        managed_file_count=managed_file_count,
        referenced_file_count=referenced_file_count,
        has_linked_issue=has_linked_issue,
        issue_status_after_delete=issue_status_after_delete,
        issue_status_reason=issue_status_reason,
    )


async def delete_library_entry(
    session: AsyncSession,
    *,
    target: Path,
    root: LibraryRoot,
    kind: str,
    delete_context: LibraryDeleteContext,
    delete_files: bool = False,
    delete_folder: bool = False,
    trash_dir: Path | None = None,
) -> LibraryDeleteOutcome:
    """Delete or trash a single library browser target and sync tracked DB state."""
    if kind == "root":
        raise ValidationError("Library roots cannot be deleted from the browser.")

    if delete_context.mode == "series":
        effective_delete_folder = True
        effective_delete_files = True
        if delete_context.series_id is None:
            raise ValidationError("Tracked series information is missing for this folder.")
        await SeriesService.delete(
            session,
            delete_context.series_id,
            delete_files=effective_delete_files,
            delete_folder=effective_delete_folder,
            trash_dir=trash_dir,
            folder_path_override=target,
        )
        return LibraryDeleteOutcome(
            kind=kind,
            mode="series",
            source_path=str(target),
            deleted_via_trash=(
                trash_dir is not None and (effective_delete_files or effective_delete_folder)
            ),
        )

    prefix = _path_prefix(target)
    file_clause = (
        LibraryFile.file_path == prefix
        if kind == "file"
        else or_(LibraryFile.file_path == prefix, LibraryFile.file_path.like(f"{prefix}/%"))
    )
    tracked_files = list(
        (
            await session.execute(
                select(LibraryFile)
                .options(selectinload(LibraryFile.issue).selectinload(Issue.series))
                .where(file_clause)
            )
        )
        .scalars()
        .all()
    )

    tracked_series: list[Series] = []
    if kind == "folder":
        tracked_series = list(
            (
                await session.execute(
                    select(Series).where(
                        or_(Series.path == prefix, Series.path.like(f"{prefix}/%"))
                    )
                )
            )
            .scalars()
            .all()
        )

    managed_files = [
        library_file
        for library_file in tracked_files
        if library_file.storage_mode == LibraryFileStorageMode.MANAGED
    ]
    referenced_files = [
        library_file
        for library_file in tracked_files
        if library_file.storage_mode == LibraryFileStorageMode.REFERENCED
    ]

    result_path: str | None = None
    deleted_via_trash = False
    if referenced_files:
        # A whole-file or whole-folder operation could mutate a referenced artifact.
        # Preserve the target and remove only Pullbox-owned files individually.
        for library_file in managed_files:
            managed_path = Path(library_file.file_path)
            if not (managed_path.exists() or managed_path.is_symlink()):
                continue
            if trash_dir is not None:
                try:
                    move_path_to_utility_trash(
                        managed_path,
                        trash_dir,
                        relative_path=_trash_relative_path(managed_path, root),
                    )
                except FileExistsError as exc:
                    raise ValidationError(str(exc)) from exc
                deleted_via_trash = True
            else:
                _delete_path_permanently(managed_path)
    elif trash_dir is not None:
        try:
            result_path = str(
                move_path_to_utility_trash(
                    target,
                    trash_dir,
                    relative_path=_trash_relative_path(target, root),
                )
            )
            deleted_via_trash = True
        except FileExistsError as exc:
            raise ValidationError(str(exc)) from exc
    else:
        _delete_path_permanently(target)

    for library_file in tracked_files:
        if library_file.issue is not None:
            library_file.issue.status = _status_after_library_file_removed(library_file.issue)
        await session.delete(library_file)

    if not referenced_files:
        for series in tracked_series:
            series.path = None

    return LibraryDeleteOutcome(
        kind=kind,
        mode=delete_context.mode,
        source_path=str(target),
        deleted_via_trash=deleted_via_trash,
        result_path=result_path,
        managed_files_deleted=len(managed_files),
        referenced_files_detached=len(referenced_files),
    )
