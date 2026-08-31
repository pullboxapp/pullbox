"""Read-only scan eligibility for Mylar files adopted under an existing root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.exceptions import ConfigurationError, ValidationError
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    build_file_identity_signature,
    resolve_referenced_source_root,
    validate_file_identity_signature,
)
from pullbox.models.library import LibraryRoot
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


async def load_mylar_in_place_root(session: AsyncSession, root_id: int | None) -> LibraryRoot:
    """Require a selected root without confusing the database with comic storage."""
    if root_id is None:
        raise ValidationError("Select an existing enabled library root for Mylar in-place import.")
    root = await session.get(LibraryRoot, root_id)
    if root is None or not root.enabled:
        raise ValidationError("Selected library root is missing or disabled.")
    try:
        await resolve_referenced_source_root(session, Path(root.path), root.id)
    except ReferencedFileValidationError as exc:
        raise ValidationError(exc.message) from exc
    return root


def _validate_file(discovered_file: DiscoveredFile, root_path: Path) -> None:
    source = Path(discovered_file.file_path)
    if ".." in source.parts or any(ord(char) < 32 or ord(char) == 127 for char in str(source)):
        raise ReferencedFileValidationError(
            "source_path_unsafe", "The Mylar comic path contains unsafe path components."
        )
    try:
        lexical_root = root_path.expanduser().absolute()
        resolved_root = root_path.expanduser().resolve(strict=True)
        lexical_source = source.expanduser().absolute()
        resolved_source = source.expanduser().resolve(strict=False)
        if not lexical_source.is_relative_to(lexical_root) or not resolved_source.is_relative_to(
            resolved_root
        ):
            raise ReferencedFileValidationError(
                "source_outside_root", "The Mylar comic file is outside the selected library root."
            )
        if not resolved_source.is_file():
            raise ReferencedFileValidationError(
                "source_missing", "The Mylar comic file is missing or unavailable."
            )
        if not os.access(resolved_source, os.R_OK):
            raise ReferencedFileValidationError(
                "source_unreadable", "The Mylar comic file is not readable by Pullbox."
            )
        current = build_file_identity_signature(source)
    except ReferencedFileValidationError:
        raise
    except (OSError, RuntimeError, ValueError, ConfigurationError) as exc:
        raise ReferencedFileValidationError(
            "source_missing", "The Mylar comic file is missing or unavailable."
        ) from exc
    validate_file_identity_signature(dict(discovered_file.source_signature), current)


def validate_mylar_in_place_files(discovered: list[DiscoveredSeries], root_path: Path) -> None:
    """Mark ineligible files for review without dropping them or copying them."""
    for series in discovered:
        failure_count = 0
        first_failure: tuple[str, str] | None = None
        for comic in series.files:
            try:
                _validate_file(comic, root_path)
            except ReferencedFileValidationError as exc:
                failure_count += 1
                if first_failure is None:
                    first_failure = exc.reason, exc.message
                comic.metadata_diagnostics["file_safety"] = build_import_safety_diagnostics(
                    exc.message,
                    kind="source_revalidation",
                    code=exc.reason,
                    source="source_revalidation",
                    overrideable_hint=False,
                )
        if first_failure is not None and failure_count == len(series.files):
            series.diagnostics.update(
                {
                    "kind": "mylar3_path_incompatible",
                    "reason": first_failure[0],
                    "rejection_reason": first_failure[1],
                }
            )
            path_details = series.diagnostics.get("mylar3_path")
            if isinstance(path_details, dict):
                path_details["status"] = "in_place_incompatible"
