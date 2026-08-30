"""Delete-target resolution helpers for series removal."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


_ARCHIVE_PROGRESS_PREFIX = "pullbox-archive-progress-"
_ARCHIVE_PROGRESS_SUFFIX = ".json"
_CV_SUFFIX_RE = re.compile(r"\[(?:cv-)?(?P<comicvine_id>\d+)\]$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SeriesDeleteTarget:
    """Resolved filesystem cleanup target for one series delete action."""

    folder_paths: tuple[Path, ...]
    linked_file_count: int
    managed_file_count: int
    referenced_file_count: int


@dataclass(frozen=True, slots=True)
class SeriesDeleteContext:
    """UI-facing delete modal context for one or more series."""

    series_count: int
    linked_file_count: int
    managed_file_count: int
    referenced_file_count: int


def is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def trash_relative_path(source: Path, root: LibraryRoot | None) -> Path:
    if root is None:
        return Path(source.name)

    root_path = Path(root.path).expanduser().resolve(strict=False)
    if source == root_path or is_relative_to(source, root_path):
        return Path(root.name) / source.relative_to(root_path)
    return Path(source.name)


def path_exists_or_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def path_key(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False)).rstrip("/")


def dedupe_existing_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    resolved: list[Path] = []
    for path in paths:
        if not path_exists_or_symlink(path):
            continue
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return tuple(resolved)


def is_archive_progress_artifact(path: Path) -> bool:
    return (
        path.is_file()
        and path.name.startswith(_ARCHIVE_PROGRESS_PREFIX)
        and path.name.endswith(_ARCHIVE_PROGRESS_SUFFIX)
    )


def reclaim_transient_series_folder(folder_path: Path) -> bool:
    """Remove a collision folder when it only contains leaked archive progress files."""
    if not folder_path.exists() or not folder_path.is_dir():
        return False

    children = list(folder_path.iterdir())
    if children and all(is_archive_progress_artifact(child) for child in children):
        shutil.rmtree(folder_path, ignore_errors=True)
        return True

    return False


async def resolve_delete_folder_paths(
    session: AsyncSession,
    series: Series,
    *,
    build_series_folder_name: Callable[[AsyncSession, Series], Awaitable[str | None]],
) -> tuple[Path, ...]:
    """Resolve likely series folders even when the stored series.path is stale."""
    candidates: list[Path] = []

    if series.path:
        candidates.append(Path(series.path).expanduser())

    root = (
        await session.get(LibraryRoot, series.library_root_id)
        if series.library_root_id is not None
        else None
    )
    if root is None:
        return dedupe_existing_paths(candidates)

    root_path = Path(root.path).expanduser()
    if not path_exists_or_symlink(root_path) or not root_path.is_dir():
        return dedupe_existing_paths(candidates)

    candidate_names: list[str] = []
    if series.path:
        candidate_names.append(Path(series.path).name)

    expected_name = await build_series_folder_name(session, series)
    if expected_name:
        candidate_names.append(expected_name)
        if series.comicvine_id is not None and _CV_SUFFIX_RE.search(expected_name) is None:
            candidate_names.append(f"{expected_name} [cv-{series.comicvine_id}]")

    seen_names: set[str] = set()
    for name in candidate_names:
        normalized = str(name or "").strip()
        if not normalized or normalized in seen_names:
            continue
        seen_names.add(normalized)
        candidates.append(root_path / normalized)

    if series.comicvine_id is not None:
        comicvine_id = int(series.comicvine_id)
        try:
            root_children = list(root_path.iterdir())
        except OSError:
            root_children = []
        for child in root_children:
            if child.name.startswith("."):
                continue
            if not (child.is_dir() or child.is_symlink()):
                continue
            match = _CV_SUFFIX_RE.search(child.name)
            if match is None or int(match.group("comicvine_id")) != comicvine_id:
                continue
            candidates.append(child)

    return dedupe_existing_paths(candidates)


async def infer_delete_folder_paths_from_linked_files(
    session: AsyncSession,
    *,
    series_id: int,
    root: LibraryRoot | None,
) -> tuple[Path, ...]:
    """Infer a removable series folder from the current linked file locations."""
    issue_ids_subq = select(Issue.id).where(Issue.series_id == series_id)
    result = await session.execute(
        select(LibraryFile.file_path).where(LibraryFile.issue_id.in_(issue_ids_subq))
    )
    parent_dirs = []
    for file_path in result.scalars().all():
        candidate = Path(file_path).expanduser().parent
        if path_exists_or_symlink(candidate):
            parent_dirs.append(candidate)

    candidates = list(parent_dirs)
    if parent_dirs:
        try:
            common_parent = Path(
                os.path.commonpath(
                    [str(path.expanduser().resolve(strict=False)) for path in parent_dirs]
                )
            )
        except ValueError:
            common_parent = None
        if common_parent is not None and common_parent.name:
            root_path = (
                Path(root.path).expanduser().resolve(strict=False) if root is not None else None
            )
            if root_path is None or common_parent != root_path:
                candidates.append(common_parent)

    return dedupe_existing_paths(candidates)


async def count_linked_existing_files(
    session: AsyncSession,
    *,
    series_id: int,
    folder_paths: tuple[Path, ...],
) -> int:
    """Count series-linked files that are still present on disk."""
    issue_ids_subq = select(Issue.id).where(Issue.series_id == series_id)
    result = await session.execute(
        select(LibraryFile.file_path).where(LibraryFile.issue_id.in_(issue_ids_subq))
    )
    file_paths = [Path(file_path).expanduser() for file_path in result.scalars().all()]

    count = 0
    for file_path in file_paths:
        if not file_path.is_file():
            continue
        if folder_paths and not any(is_relative_to(file_path, folder) for folder in folder_paths):
            continue
        count += 1
    return count


async def count_linked_existing_files_by_storage(
    session: AsyncSession,
    *,
    series_id: int,
    folder_paths: tuple[Path, ...],
) -> tuple[int, int]:
    """Count existing managed and referenced files included in a delete target."""
    issue_ids_subq = select(Issue.id).where(Issue.series_id == series_id)
    result = await session.execute(
        select(LibraryFile.file_path, LibraryFile.storage_mode).where(
            LibraryFile.issue_id.in_(issue_ids_subq)
        )
    )
    managed_count = 0
    referenced_count = 0
    for file_path_value, storage_mode in result.all():
        file_path = Path(file_path_value).expanduser()
        if not file_path.is_file():
            continue
        if folder_paths and not any(is_relative_to(file_path, folder) for folder in folder_paths):
            continue
        if storage_mode == LibraryFileStorageMode.REFERENCED:
            referenced_count += 1
        else:
            managed_count += 1
    return managed_count, referenced_count


async def build_series_delete_target(
    session: AsyncSession,
    series: Series,
    *,
    build_series_folder_name: Callable[[AsyncSession, Series], Awaitable[str | None]],
) -> SeriesDeleteTarget:
    """Resolve the actual delete target for one series."""
    folder_paths = await resolve_delete_folder_paths(
        session,
        series,
        build_series_folder_name=build_series_folder_name,
    )
    root = (
        await session.get(LibraryRoot, series.library_root_id)
        if series.library_root_id is not None
        else None
    )
    if not folder_paths:
        folder_paths = await infer_delete_folder_paths_from_linked_files(
            session,
            series_id=series.id,
            root=root,
        )
    linked_file_count = await count_linked_existing_files(
        session,
        series_id=series.id,
        folder_paths=folder_paths,
    )
    managed_file_count, referenced_file_count = await count_linked_existing_files_by_storage(
        session,
        series_id=series.id,
        folder_paths=folder_paths,
    )
    return SeriesDeleteTarget(
        folder_paths=folder_paths,
        linked_file_count=linked_file_count,
        managed_file_count=managed_file_count,
        referenced_file_count=referenced_file_count,
    )


async def build_series_delete_context(
    session: AsyncSession,
    series_ids: list[int],
    *,
    target_builder: Callable[[AsyncSession, Series], Awaitable[SeriesDeleteTarget]],
) -> SeriesDeleteContext:
    """Build delete-modal UI state for one or more series."""
    linked_file_count = 0
    managed_file_count = 0
    referenced_file_count = 0
    loaded_count = 0

    for series_id in dict.fromkeys(series_ids):
        series = await session.get(Series, int(series_id))
        if series is None:
            continue
        target = await target_builder(session, series)
        linked_file_count += target.linked_file_count
        managed_file_count += target.managed_file_count
        referenced_file_count += target.referenced_file_count
        loaded_count += 1

    return SeriesDeleteContext(
        series_count=loaded_count,
        linked_file_count=linked_file_count,
        managed_file_count=managed_file_count,
        referenced_file_count=referenced_file_count,
    )
