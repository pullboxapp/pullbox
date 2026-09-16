"""Library target path planning helpers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.exceptions import ConfigurationError, ImportDestinationValidationError
from pullbox.core.library_naming import (
    build_series_relative_path,
    compute_target_filename,
    resolve_naming_issue_type,
)
from pullbox.models.series import Series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryRoot


@dataclass(frozen=True, slots=True)
class ResolvedLibraryTarget:
    """Resolved destination path plus folder-creation metadata."""

    path: Path
    series_folder_created: bool
    created_directory_paths: tuple[Path, ...] = ()
    directory_ownership_boundary_path: Path | None = None


async def resolve_library_target_path(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    ingest_policy: LibraryIngestPolicy,
    rename: bool,
    *,
    replace_existing_path: Path | None = None,
    source_scan_root: Path | None = None,
    strict_import: bool = False,
) -> ResolvedLibraryTarget:
    """Resolve the final library path before materializing the artifact."""
    target_path = await predict_library_target_path(
        session,
        source_path,
        issue,
        series,
        root,
        ingest_policy,
        rename,
    )
    series_folder = target_path.parent

    comics_dir = Path(root.path)
    if not comics_dir.exists():
        raise ConfigurationError(f"Comics directory does not exist: {comics_dir}")

    if strict_import:
        _validate_strict_import_target(
            source_path,
            target_path,
            comics_dir=comics_dir,
            source_scan_root=source_scan_root,
        )

    created_directory_paths = await asyncio.to_thread(
        _create_target_directories,
        series_folder,
        comics_dir,
    )
    series_folder_created = series_folder in created_directory_paths

    target_is_replaceable = replace_existing_path is not None and target_path.resolve(
        strict=False
    ) == replace_existing_path.resolve(strict=False)
    if (
        not strict_import
        and target_path.exists()
        and target_path != source_path
        and not target_is_replaceable
    ):
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = series_folder / f"{stem} ({counter}){suffix}"
            counter += 1

    return ResolvedLibraryTarget(
        path=target_path,
        series_folder_created=series_folder_created,
        created_directory_paths=created_directory_paths,
        directory_ownership_boundary_path=comics_dir,
    )


def _create_target_directories(directory: Path, boundary: Path) -> tuple[Path, ...]:
    """Create target directories one at a time and return only paths we created.

    ``mkdir(parents=True)`` cannot report which missing ancestors it created. A
    rollback therefore could not distinguish an import-owned publisher folder
    from an empty folder that existed before the import. Creating each segment
    individually gives the rollback journal exact, race-aware ownership.
    """
    try:
        relative = directory.relative_to(boundary)
    except ValueError:
        # Existing series paths may be expressed through a lexical alias while
        # resolving inside the configured root. Preserve compatibility without
        # claiming ownership that cannot be proven from this path expression.
        directory.mkdir(parents=True, exist_ok=True)
        return ()

    created: list[Path] = []
    current = boundary
    for segment in relative.parts:
        current /= segment
        try:
            current.mkdir()
        except FileExistsError:
            if not current.is_dir():
                raise
        else:
            created.append(current)
    return tuple(created)


def _validate_strict_import_target(
    source_path: Path,
    target_path: Path,
    *,
    comics_dir: Path,
    source_scan_root: Path | None,
) -> None:
    """Fail closed when an import target is not a new, disjoint artifact path."""
    resolved_source = source_path.expanduser().resolve(strict=False)
    resolved_target = target_path.expanduser().resolve(strict=False)
    if resolved_source == resolved_target or _same_existing_file(source_path, target_path):
        raise ImportDestinationValidationError(
            "source_destination_same",
            "Managed import source and destination resolve to the same file. "
            "Choose Keep files in place or a different managed library root.",
        )

    if source_scan_root is not None:
        resolved_source_root = source_scan_root.expanduser().resolve(strict=False)
        target_inside_source = (
            resolved_target == resolved_source_root
            or resolved_target.is_relative_to(resolved_source_root)
        )
        root_aliases_source = _same_existing_file(comics_dir, source_scan_root)
        if target_inside_source or root_aliases_source:
            raise ImportDestinationValidationError(
                "destination_inside_source",
                "Managed library destination is inside the import source or aliases its "
                "inventory boundary. Choose Keep files in place or a non-overlapping "
                "library root.",
            )

    collision_name = _casefold_collision_name(target_path)
    if collision_name is None:
        return
    if collision_name != target_path.name:
        raise ImportDestinationValidationError(
            "destination_case_collision",
            f"Managed import target has a case-insensitive collision: {collision_name}. "
            "Review the existing library artifact before retrying.",
        )
    raise ImportDestinationValidationError(
        "destination_collision",
        "Managed import target already exists. Review the existing library artifact "
        "before retrying.",
    )


def _same_existing_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError, ValueError):
        return False


def _casefold_collision_name(target_path: Path) -> str | None:
    parent = target_path.parent
    if not parent.exists():
        return None
    target_key = target_path.name.casefold()
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name.casefold() == target_key:
                    return entry.name
    except OSError as exc:
        raise ConfigurationError(
            f"Could not verify the managed import target directory: {parent}"
        ) from exc
    return None


async def predict_library_target_path(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    ingest_policy: LibraryIngestPolicy,
    rename: bool,
) -> Path:
    """Compute the canonical library target path without applying collision suffixes."""
    comics_dir = Path(root.path)
    if not comics_dir.exists():
        raise ConfigurationError(f"Comics directory does not exist: {comics_dir}")

    use_existing_series_folder = False
    if isinstance(series, Series) and series.path:
        current_series_folder = Path(series.path)
        try:
            current_series_folder.resolve(strict=False).relative_to(
                comics_dir.resolve(strict=False)
            )
        except ValueError as exc:
            if series.library_root_id == root.id:
                raise ConfigurationError(
                    "Existing series path is outside its library root."
                ) from exc
        else:
            series_folder = current_series_folder
            use_existing_series_folder = True

    if not use_existing_series_folder:
        try:
            relative_series_path = build_series_relative_path(series, ingest_policy)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        series_folder = comics_dir / relative_series_path
        try:
            series_folder.resolve(strict=False).relative_to(comics_dir.resolve(strict=False))
        except ValueError as exc:
            raise ConfigurationError("Rendered series path is outside its library root.") from exc

    if rename:
        effective_issue_type = await resolve_naming_issue_type(session, issue)
        target_name = compute_target_filename(
            issue,
            series,
            source_path,
            ingest_policy,
            issue_type_override=effective_issue_type,
        )
    else:
        target_name = source_path.name

    return series_folder / target_name
