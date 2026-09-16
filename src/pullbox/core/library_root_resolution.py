"""Library root selection helpers for file registration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import ConfigurationError
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


def preferred_managed_root_id(series: object) -> int | None:
    """Return only an explicitly selected future managed destination."""
    root_id = getattr(series, "preferred_library_root_id", None)
    return root_id if isinstance(root_id, int) and not isinstance(root_id, bool) else None


async def resolve_library_root(
    session: AsyncSession,
    source_path: Path,
    explicit_root_id: int | None,
    *,
    series: Series | None = None,
) -> LibraryRoot:
    """Resolve the writable destination root for a managed library file.

    ``source_path`` remains part of the public call signature for compatibility,
    but an import source does not confer library ownership or choose where a
    managed artifact is written.
    """
    if explicit_root_id is not None:
        root = await session.get(LibraryRoot, explicit_root_id)
        return _require_managed_root(root, label="Selected library root")

    if series is not None:
        if series.preferred_library_root_id is not None:
            root = await session.get(LibraryRoot, series.preferred_library_root_id)
            return _require_managed_root(root, label="Series preferred library root")
        if series.library_root_id is not None:
            root = await session.get(LibraryRoot, series.library_root_id)
            if root is None:
                raise ConfigurationError("Series library root does not exist.")
            if root.enabled and root.allow_managed_writes:
                return root
        if series.path:
            roots = list((await session.execute(select(LibraryRoot))).scalars().all())
            containing_roots = [
                root for root in roots if path_is_inside_root(Path(series.path), root)
            ]
            if len(containing_roots) > 1:
                raise ConfigurationError("Series path matches multiple library roots.")
            if containing_roots:
                containing_root = containing_roots[0]
                if containing_root.enabled and containing_root.allow_managed_writes:
                    return containing_root
            elif series.library_root_id is None:
                raise ConfigurationError(
                    "Series path does not belong to a configured library root."
                )

    default_roots_result = await session.execute(
        select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True))
    )
    default_roots = list(default_roots_result.scalars().all())
    if len(default_roots) > 1:
        raise ConfigurationError(
            "Multiple library roots are configured as the default destination."
        )
    if default_roots:
        return _require_managed_root(
            default_roots[0],
            label="Default managed library root",
        )

    config_result = await session.execute(
        select(SystemConfig).where(SystemConfig.key == "comics_directory")
    )
    config = config_result.scalars().first()
    if config is not None and config.value.strip():
        configured_path = _resolved_path(config.value)
        roots_result = await session.execute(select(LibraryRoot))
        matching_roots = [
            root
            for root in roots_result.scalars().all()
            if _resolved_path(root.path) == configured_path
        ]
        if len(matching_roots) > 1:
            raise ConfigurationError("The legacy comics directory matches multiple library roots.")
        if matching_roots:
            return _require_managed_root(
                matching_roots[0],
                label="Legacy comics directory root",
            )

    raise ConfigurationError(
        "No managed library destination is configured. Set a default root in Settings → Media."
    )


def _require_managed_root(root: LibraryRoot | None, *, label: str) -> LibraryRoot:
    """Require a resolved root to be available for managed placement."""
    if root is None:
        raise ConfigurationError(f"{label} does not exist.")
    if not root.enabled:
        raise ConfigurationError(f"{label} is disabled.")
    if not root.allow_managed_writes:
        raise ConfigurationError(f"{label} does not allow managed writes.")
    return root


def _resolved_path(path: str | Path) -> Path:
    """Return one normalized path value for identity-safe comparisons."""
    return Path(path).expanduser().resolve(strict=False)


def path_is_inside_root(path: Path, root: LibraryRoot) -> bool:
    """Return true when a candidate path is inside a library root."""
    root_path = _resolved_path(root.path)
    candidate = _resolved_path(path)
    return candidate == root_path or candidate.is_relative_to(root_path)


def resolve_path_inside_roots(
    path: str | Path,
    roots: Iterable[str | Path],
    *,
    require_exists: bool = False,
    require_file: bool = False,
    require_dir: bool = False,
) -> Path:
    """Resolve a path and require it to stay inside one of the supplied roots."""
    root_paths = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
    if not root_paths:
        raise ValueError("No allowed roots are configured.")

    # This is the central path validator; the candidate is immediately checked
    # against enabled library roots before any caller can use the result.
    # codeql[py/path-injection]
    candidate = Path(path).expanduser().resolve(strict=False)
    if not any(candidate == root or candidate.is_relative_to(root) for root in root_paths):
        raise ValueError(f"Selected path is outside enabled library roots: {path}")

    if require_exists and not candidate.exists():
        raise ValueError(f"Selected path does not exist: {path}")
    if require_file and not candidate.is_file():
        raise ValueError(f"Selected path is not a file: {path}")
    if require_dir and not candidate.is_dir():
        raise ValueError(f"Selected path is not a directory: {path}")

    return candidate


def materialize_series_path(
    series: object,
    series_folder: Path,
    root: LibraryRoot,
    *,
    storage_mode: LibraryFileStorageMode,
) -> None:
    """Persist the actual library folder once the first file lands there."""
    if not isinstance(series, Series):
        return
    if not series.path:
        series.path = str(series_folder)
    if series.library_root_id is None:
        series.library_root_id = root.id
    if series.preferred_library_root_id is None and storage_mode == LibraryFileStorageMode.MANAGED:
        series.preferred_library_root_id = root.id
