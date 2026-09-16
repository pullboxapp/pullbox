"""Server-side validation for confirmed Mylar path mappings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.core.filesystem_policy import is_sensitive_path
from pullbox.models.import_job import ImportFileHandlingMode
from pullbox.models.library import LibraryRoot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def validate_mylar3_path_map_targets(
    session: AsyncSession,
    path_map: dict[str, str],
    *,
    file_handling_mode: ImportFileHandlingMode,
) -> None:
    """Validate readable Mylar source mappings for the selected ownership mode."""
    if not path_map:
        return

    available_roots: list[tuple[Path, Path]] = []
    if file_handling_mode == ImportFileHandlingMode.IN_PLACE:
        roots = list(
            (
                await session.execute(
                    select(LibraryRoot)
                    .where(
                        LibraryRoot.enabled.is_(True),
                        LibraryRoot.allow_referenced_registrations.is_(True),
                    )
                    .order_by(LibraryRoot.id.asc())
                )
            )
            .scalars()
            .all()
        )
        available_roots = _available_root_boundaries(roots)
    for visible_prefix in path_map.values():
        target = Path(visible_prefix)
        if not target.is_absolute() or ".." in target.parts or target == Path("/"):
            raise ValidationError(
                "Each Pullbox-visible Mylar mapping path must be a safe absolute directory."
            )
        try:
            lexical_target = target
            # This operator-confirmed source is bounded above, resolved before
            # containment checks, and screened against sensitive paths below.
            # Managed-copy imports intentionally allow external source roots.
            # codeql[py/path-injection]
            resolved_target = target.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValidationError(
                "Each Pullbox-visible Mylar mapping path must be an available directory."
            ) from exc
        if is_sensitive_path(resolved_target):
            raise ValidationError(
                "Each Pullbox-visible Mylar mapping path must be a safe source directory."
            )
        if not resolved_target.is_dir() or not os.access(resolved_target, os.R_OK | os.X_OK):
            raise ValidationError(
                "Each Pullbox-visible Mylar mapping path must be a readable directory."
            )
        if file_handling_mode == ImportFileHandlingMode.IN_PLACE and not any(
            lexical_target.is_relative_to(lexical_root)
            and resolved_target.is_relative_to(resolved_root)
            for lexical_root, resolved_root in available_roots
        ):
            raise ValidationError(
                "Each Pullbox-visible Mylar mapping path must be inside an enabled library root."
            )


def _available_root_boundaries(roots: list[LibraryRoot]) -> list[tuple[Path, Path]]:
    boundaries: list[tuple[Path, Path]] = []
    for root in roots:
        try:
            lexical_root = Path(root.path).expanduser().absolute()
            resolved_root = Path(root.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved_root.is_dir() and os.access(resolved_root, os.R_OK):
            boundaries.append((lexical_root, resolved_root))
    return boundaries
