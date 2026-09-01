"""Read-only scan eligibility for Mylar files adopted across configured roots."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import ConfigurationError, ValidationError
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.core.library_root_resolution import resolve_library_root
from pullbox.models.library import LibraryRoot
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics
from pullbox.services.library_root_management import validate_managed_library_root

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY = "mylar_reference_root_id"
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class MylarReferenceRootBoundary:
    """Immutable scan-time containment evidence for one reference-capable root."""

    root_id: int
    lexical: Path
    resolved: Path
    device: int
    inode: int


async def load_mylar_in_place_root(session: AsyncSession, root_id: int | None) -> LibraryRoot:
    """Require the preferred future managed destination for an in-place import."""
    if root_id is None:
        raise ValidationError(
            "Select an enabled managed-write library root for Mylar in-place import."
        )
    try:
        root = await resolve_library_root(session, Path(), root_id)
    except ConfigurationError as exc:
        raise ValidationError(exc.message) from exc
    await validate_managed_library_root(root)
    return root


async def load_mylar_reference_root_boundaries(
    session: AsyncSession,
) -> tuple[MylarReferenceRootBoundary, ...]:
    """Load the current enabled roots that explicitly allow references."""
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
    root_values = [(int(root.id), str(root.path)) for root in roots]
    return await asyncio.to_thread(_snapshot_root_boundaries, root_values)


def _snapshot_root_boundaries(
    roots: Sequence[tuple[int, str]],
) -> tuple[MylarReferenceRootBoundary, ...]:
    boundaries: list[MylarReferenceRootBoundary] = []
    for root_id, raw_path in roots:
        path = Path(raw_path)
        if not path.is_absolute() or ".." in path.parts or _CONTROL_CHARACTER_RE.search(raw_path):
            continue
        try:
            lexical = path.expanduser().absolute()
            resolved = path.expanduser().resolve(strict=True)
            stat_result = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.X_OK):
            continue
        boundaries.append(
            MylarReferenceRootBoundary(
                root_id=root_id,
                lexical=lexical,
                resolved=resolved,
                device=stat_result.st_dev,
                inode=stat_result.st_ino,
            )
        )
    return tuple(boundaries)


def _select_file_root(
    source: Path,
    roots: Sequence[MylarReferenceRootBoundary],
) -> tuple[MylarReferenceRootBoundary, dict[str, int | str]]:
    raw_source = str(source)
    if not source.is_absolute() or ".." in source.parts or _CONTROL_CHARACTER_RE.search(raw_source):
        raise ReferencedFileValidationError(
            "source_path_unsafe", "The Mylar comic path contains unsafe path components."
        )
    try:
        lexical_source = source.expanduser().absolute()
        resolved_source = source.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReferencedFileValidationError(
            "source_missing", "The Mylar comic file is missing or unavailable."
        ) from exc
    if not resolved_source.is_file():
        raise ReferencedFileValidationError(
            "source_missing", "The Mylar comic file is missing or unavailable."
        )
    if not os.access(resolved_source, os.R_OK):
        raise ReferencedFileValidationError(
            "source_unreadable", "The Mylar comic file is not readable by Pullbox."
        )

    lexical_claims = [root for root in roots if lexical_source.is_relative_to(root.lexical)]
    resolved_claims = [root for root in roots if resolved_source.is_relative_to(root.resolved)]
    resolved_claim_ids = {root.root_id for root in resolved_claims}
    candidates = [root for root in lexical_claims if root.root_id in resolved_claim_ids]
    candidates.sort(
        key=lambda root: (len(root.lexical.parts), len(root.resolved.parts), -root.root_id),
        reverse=True,
    )
    if not candidates:
        raise ReferencedFileValidationError(
            "source_outside_root",
            "The Mylar comic file is outside every enabled reference-capable library root.",
        )
    selected = candidates[0]
    selected_aliases = [
        root
        for root in roots
        if root.root_id != selected.root_id
        and (root.device, root.inode) == (selected.device, selected.inode)
    ]
    if (
        len(candidates) != 1
        or len(lexical_claims) != 1
        or len(resolved_claims) != 1
        or selected_aliases
    ):
        raise ReferencedFileValidationError(
            "source_root_ambiguous",
            "The Mylar comic file matches ambiguous nested or aliased library roots.",
        )
    return selected, build_file_identity_signature(resolved_source)


def _validate_file(
    discovered_file: DiscoveredFile,
    roots: Sequence[MylarReferenceRootBoundary],
) -> None:
    source = Path(discovered_file.file_path)
    try:
        selected, current = _select_file_root(source, roots)
    except ReferencedFileValidationError:
        raise
    except (OSError, RuntimeError, ValueError, ConfigurationError) as exc:
        raise ReferencedFileValidationError(
            "source_missing", "The Mylar comic file is missing or unavailable."
        ) from exc
    validate_file_identity_signature(dict(discovered_file.source_signature), current)
    signature = dict(discovered_file.source_signature)
    signature[MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY] = selected.root_id
    discovered_file.source_signature = signature


def validate_mylar_in_place_files(
    discovered: list[DiscoveredSeries],
    roots: Sequence[MylarReferenceRootBoundary],
) -> None:
    """Mark ineligible files for review without dropping them or copying them."""
    for series in discovered:
        failure_count = 0
        first_failure: tuple[str, str] | None = None
        for comic in series.files:
            try:
                _validate_file(comic, roots)
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


async def revalidate_mylar_in_place_file_root(
    session: AsyncSession,
    source_path: Path,
    source_signature: dict[str, object],
) -> int:
    """Revalidate the exact scan-selected root immediately before registration."""
    raw_root_id = source_signature.get(MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY)
    if isinstance(raw_root_id, bool) or not isinstance(raw_root_id, int) or raw_root_id <= 0:
        raise ReferencedFileValidationError(
            "source_root_unconfirmed",
            "The Mylar comic file is missing its confirmed library-root selection. Rescan it.",
        )
    roots = await load_mylar_reference_root_boundaries(session)
    selected, current = await asyncio.to_thread(_select_file_root, source_path, roots)
    if selected.root_id != raw_root_id:
        raise ReferencedFileValidationError(
            "source_root_changed",
            "The Mylar comic file no longer resolves inside its scan-selected library root.",
        )
    validate_file_identity_signature(source_signature, current)
    return selected.root_id
