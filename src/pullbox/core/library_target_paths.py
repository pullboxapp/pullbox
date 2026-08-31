"""Library target path planning helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.exceptions import ConfigurationError
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

    series_folder_created = not series_folder.exists()
    await asyncio.to_thread(series_folder.mkdir, parents=True, exist_ok=True)

    target_is_replaceable = replace_existing_path is not None and target_path.resolve(
        strict=False
    ) == replace_existing_path.resolve(strict=False)
    if target_path.exists() and target_path != source_path and not target_is_replaceable:
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = series_folder / f"{stem} ({counter}){suffix}"
            counter += 1

    return ResolvedLibraryTarget(
        path=target_path,
        series_folder_created=series_folder_created,
    )


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

    if isinstance(series, Series) and series.path:
        series_folder = Path(series.path)
        if series.library_root_id is not None and series.library_root_id != root.id:
            raise ConfigurationError("Series belongs to a different library root.")
        try:
            series_folder.resolve(strict=False).relative_to(comics_dir.resolve(strict=False))
        except ValueError as exc:
            raise ConfigurationError("Existing series path is outside its library root.") from exc
    else:
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
