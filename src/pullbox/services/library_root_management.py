"""Explicit multi-library root management and live capability validation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select, update

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import ValidationError
from pullbox.core.filesystem_policy import is_sensitive_path
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import ImportJob
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArcPlacement
from pullbox.services.import_workflow_state import ACTIVE_IMPORT_JOB_STATUSES
from pullbox.services.library_root_policy_service import LibraryRootNotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

_LOW_CAPACITY_BYTES = 1024**3
_MUTATION_BLOCKED_MESSAGE = "Library roots cannot be changed while an import is active."
_REBIND_TOKEN_SALT = "library-root-rebind-preview-v1"
_REBIND_TOKEN_MAX_AGE_SECONDS = 15 * 60
_REBIND_TOKEN_VERSION = 1
_REBIND_ACTION = "rebind_library_root"
_REBIND_PATH_PAGE_SIZE = 2_000
_MUTABLE_ROOT_FIELDS = frozenset(
    {
        "name",
        "enabled",
        "allow_referenced_registrations",
        "allow_managed_writes",
        "is_default_managed_destination",
    }
)


@dataclass(frozen=True, slots=True)
class _RootProbe:
    display_path: Path
    resolved_path: Path | None
    identity: tuple[int, int] | None
    available: bool
    readable: bool
    writable: bool
    free_bytes: int | None
    status: str
    warnings: tuple[str, ...]
    blocking_reasons: tuple[str, ...]


async def list_library_roots(session: AsyncSession) -> list[dict[str, Any]]:
    """Return configured roots with non-persisted live capability snapshots."""
    roots = list((await session.scalars(select(LibraryRoot).order_by(LibraryRoot.id.asc()))).all())
    probes = await asyncio.gather(
        *(
            asyncio.to_thread(
                _probe_configured_root,
                root.path,
                require_write_probe=False,
            )
            for root in roots
        )
    )
    return [
        _serialize_root(
            root,
            probe,
            extra_warnings=_root_conflicts(
                roots,
                name=root.name,
                probe=probe,
                exclude_root_id=root.id,
            ),
        )
        for root, probe in zip(roots, probes, strict=True)
    ]


async def validate_managed_library_root(root: LibraryRoot) -> dict[str, Any]:
    """Fail closed unless a configured root is a live managed destination.

    Callers use this immediately before freezing or executing managed placement
    so an enabled database row cannot mask an offline or newly read-only mount.
    Low capacity remains visible as a warning; workload-specific capacity checks
    belong to the import preflight that knows the required byte count.
    """
    if not root.enabled:
        raise ValidationError("The selected managed library root is disabled.")
    if not root.allow_managed_writes:
        raise ValidationError("The selected library root does not allow managed writes.")
    probe = await asyncio.to_thread(
        _probe_configured_root,
        root.path,
        require_write_probe=True,
    )
    blockers = list(probe.blocking_reasons)
    blockers.extend(
        _capability_blockers(
            probe,
            allow_referenced_registrations=False,
            allow_managed_writes=True,
        )
    )
    _raise_blockers(blockers)
    return _capability_dict(probe)


async def validate_reference_library_root(root: LibraryRoot) -> dict[str, Any]:
    """Fail closed unless a configured root can register referenced artifacts."""
    if not root.enabled:
        raise ValidationError("The selected reference library root is disabled.")
    if not root.allow_referenced_registrations:
        raise ValidationError("The selected library root does not allow referenced registrations.")
    probe = await asyncio.to_thread(
        _probe_configured_root,
        root.path,
        require_write_probe=False,
    )
    blockers = list(probe.blocking_reasons)
    blockers.extend(
        _capability_blockers(
            probe,
            allow_referenced_registrations=True,
            allow_managed_writes=False,
        )
    )
    _raise_blockers(blockers)
    return _capability_dict(probe)


async def preview_library_root(
    session: AsyncSession,
    *,
    name: str,
    path: str,
    allow_referenced_registrations: bool,
    allow_managed_writes: bool,
    is_default_managed_destination: bool,
) -> dict[str, Any]:
    """Validate a proposed root without writing database or filesystem state."""
    normalized_name = _normalize_name(name)
    _validate_roles(
        allow_referenced_registrations=allow_referenced_registrations,
        allow_managed_writes=allow_managed_writes,
        is_default_managed_destination=is_default_managed_destination,
    )
    probe = await asyncio.to_thread(
        _probe_candidate_root,
        path,
        require_write_probe=allow_managed_writes,
    )
    roots = list((await session.scalars(select(LibraryRoot).order_by(LibraryRoot.id))).all())
    conflicts = _root_conflicts(
        roots,
        name=normalized_name,
        probe=probe,
        exclude_root_id=None,
    )
    blockers = list(probe.blocking_reasons)
    blockers.extend(conflicts)
    blockers.extend(
        _capability_blockers(
            probe,
            allow_referenced_registrations=allow_referenced_registrations,
            allow_managed_writes=allow_managed_writes,
        )
    )
    blockers = _deduplicate(blockers)
    becomes_default = is_default_managed_destination or (
        allow_managed_writes and not any(root.is_default_managed_destination for root in roots)
    )
    capabilities = _capability_dict(probe)
    warnings = list(capabilities["warnings"])
    if not blockers and becomes_default and not is_default_managed_destination:
        warnings.append("The first managed root will become the default destination.")
    return {
        "name": normalized_name,
        "path": str(probe.display_path),
        "allow_referenced_registrations": allow_referenced_registrations,
        "allow_managed_writes": allow_managed_writes,
        "is_default_managed_destination": becomes_default,
        **capabilities,
        "warnings": _deduplicate(warnings),
        "blocking_reasons": blockers,
        "can_create": not blockers,
    }


async def create_library_root(
    session: AsyncSession,
    *,
    name: str,
    path: str,
    allow_referenced_registrations: bool,
    allow_managed_writes: bool,
    is_default_managed_destination: bool,
) -> dict[str, Any]:
    """Create a validated root and atomically maintain the one-default invariant."""
    normalized_name = _normalize_name(name)
    _validate_roles(
        allow_referenced_registrations=allow_referenced_registrations,
        allow_managed_writes=allow_managed_writes,
        is_default_managed_destination=is_default_managed_destination,
    )
    probe = await asyncio.to_thread(
        _probe_candidate_root,
        path,
        require_write_probe=allow_managed_writes,
    )
    await _assert_no_active_import(session)
    roots = list(
        (
            await session.scalars(
                select(LibraryRoot).order_by(LibraryRoot.id.asc()).with_for_update()
            )
        ).all()
    )
    blockers = list(probe.blocking_reasons)
    blockers.extend(
        _root_conflicts(
            roots,
            name=normalized_name,
            probe=probe,
            exclude_root_id=None,
        )
    )
    blockers.extend(
        _capability_blockers(
            probe,
            allow_referenced_registrations=allow_referenced_registrations,
            allow_managed_writes=allow_managed_writes,
        )
    )
    _raise_blockers(blockers)
    await _assert_no_active_import(session)

    becomes_default = is_default_managed_destination or (
        allow_managed_writes and not any(root.is_default_managed_destination for root in roots)
    )
    if becomes_default:
        await _clear_default(session)
    root = LibraryRoot(
        name=normalized_name,
        path=str(probe.display_path),
        enabled=True,
        allow_referenced_registrations=allow_referenced_registrations,
        allow_managed_writes=allow_managed_writes,
        is_default_managed_destination=becomes_default,
    )
    session.add(root)
    await session.flush()
    if becomes_default:
        await _sync_legacy_default(session, root.path)
    return _serialize_root(root, probe)


async def update_library_root(
    session: AsyncSession,
    library_root_id: int,
    changes: Mapping[str, object],
) -> dict[str, Any]:
    """Update mutable flags while keeping the root path immutable."""
    if not changes:
        raise ValidationError("At least one library root field must be changed.")
    if "path" in changes:
        raise ValidationError("Library root paths cannot be changed by this endpoint.")
    unknown_fields = set(changes) - _MUTABLE_ROOT_FIELDS
    if unknown_fields:
        raise ValidationError("Unknown library root update field.")
    if any(value is None for value in changes.values()):
        raise ValidationError("Library root update fields cannot be null.")

    await _assert_no_active_import(session)
    current = await session.get(LibraryRoot, library_root_id)
    if current is None:
        raise LibraryRootNotFoundError()

    proposed_name = _normalize_name(str(changes.get("name", current.name)))
    proposed_enabled = bool(changes.get("enabled", current.enabled))
    proposed_referenced = bool(
        changes.get(
            "allow_referenced_registrations",
            current.allow_referenced_registrations,
        )
    )
    proposed_managed = bool(changes.get("allow_managed_writes", current.allow_managed_writes))
    proposed_default = bool(
        changes.get(
            "is_default_managed_destination",
            current.is_default_managed_destination,
        )
    )
    if current.is_default_managed_destination and (
        not proposed_enabled or not proposed_managed or not proposed_default
    ):
        raise ValidationError(
            "Select another root as the default managed destination before disabling "
            "or demoting this root."
        )
    _validate_roles(
        allow_referenced_registrations=proposed_referenced,
        allow_managed_writes=proposed_managed,
        is_default_managed_destination=proposed_default,
    )
    if proposed_default and not proposed_enabled:
        raise ValidationError("The default managed destination must be enabled.")

    role_activation = proposed_enabled and (
        (proposed_referenced and not current.allow_referenced_registrations)
        or (proposed_managed and not current.allow_managed_writes)
        or (proposed_enabled and not current.enabled)
        or (proposed_default and not current.is_default_managed_destination)
    )
    probe = await asyncio.to_thread(
        _probe_configured_root,
        current.path,
        require_write_probe=role_activation and proposed_managed,
    )
    if role_activation:
        blockers = list(probe.blocking_reasons)
        blockers.extend(
            _capability_blockers(
                probe,
                allow_referenced_registrations=proposed_referenced,
                allow_managed_writes=proposed_managed,
            )
        )
        _raise_blockers(blockers)

    roots = list(
        (
            await session.scalars(
                select(LibraryRoot).order_by(LibraryRoot.id.asc()).with_for_update()
            )
        ).all()
    )
    root = next((item for item in roots if item.id == library_root_id), None)
    if root is None:
        raise LibraryRootNotFoundError()
    name_conflicts = [
        item
        for item in roots
        if item.id != root.id and item.name.strip().casefold() == proposed_name.casefold()
    ]
    if name_conflicts:
        raise ValidationError("Library root names must be unique, ignoring case.")
    await _assert_no_active_import(session)

    was_default = root.is_default_managed_destination
    root.name = proposed_name
    root.enabled = proposed_enabled
    root.allow_referenced_registrations = proposed_referenced
    root.allow_managed_writes = proposed_managed

    should_become_default = proposed_default and not was_default
    if not any(item.is_default_managed_destination for item in roots) and (
        root.enabled and root.allow_managed_writes
    ):
        should_become_default = True
    if should_become_default:
        await _clear_default(session)
        root.is_default_managed_destination = True
    else:
        root.is_default_managed_destination = proposed_default
    await session.flush()
    if root.is_default_managed_destination:
        await _sync_legacy_default(session, root.path)
    return _serialize_root(root, probe)


async def preview_library_root_rebind(
    session: AsyncSession,
    library_root_id: int,
    *,
    replacement_path: str,
    actor_id: int,
) -> dict[str, Any]:
    """Preview an explicit path-identity rebind without persisting changes."""
    roots = await _load_library_roots_for_rebind(session, lock=False)
    preview, _snapshot = await _build_library_root_rebind_preview(
        session,
        roots,
        library_root_id=library_root_id,
        replacement_path=replacement_path,
        actor_id=actor_id,
        issue_token=True,
    )
    return preview


async def rebind_library_root(
    session: AsyncSession,
    library_root_id: int,
    *,
    replacement_path: str,
    preview_token: str,
    actor_id: int,
) -> dict[str, Any]:
    """Apply one signed, drift-checked root path rebind without rewriting file paths."""
    normalized_replacement = str(_normalize_candidate_path(replacement_path))
    token_payload = _load_rebind_preview_token(preview_token)
    token_snapshot = _validate_rebind_token_scope(
        token_payload,
        library_root_id=library_root_id,
        replacement_path=normalized_replacement,
        actor_id=actor_id,
    )

    roots = await _load_library_roots_for_rebind(session, lock=True)
    preview, current_snapshot = await _build_library_root_rebind_preview(
        session,
        roots,
        library_root_id=library_root_id,
        replacement_path=normalized_replacement,
        actor_id=actor_id,
        issue_token=False,
    )
    if preview["blocking_reasons"]:
        raise ValidationError(
            "The library root rebind is no longer safe. Preview it again.",
            details={"blocking_reasons": preview["blocking_reasons"]},
        )
    if token_snapshot != current_snapshot:
        raise ValidationError("The library root changed after preview. Preview the rebind again.")

    root = next((item for item in roots if item.id == library_root_id), None)
    if root is None:
        raise LibraryRootNotFoundError()
    await _assert_no_active_import(session)

    # Re-probe the exact confirmed replacement immediately before persistence.
    post_probe = await asyncio.to_thread(
        _probe_candidate_root,
        normalized_replacement,
        require_write_probe=root.allow_managed_writes,
    )
    post_blockers = list(post_probe.blocking_reasons)
    post_blockers.extend(
        _capability_blockers(
            post_probe,
            allow_referenced_registrations=root.allow_referenced_registrations,
            allow_managed_writes=root.allow_managed_writes,
        )
    )
    _raise_blockers(post_blockers)

    root.path = str(post_probe.display_path)
    await session.flush()
    if root.is_default_managed_destination:
        await _sync_legacy_default(session, root.path)
    return _serialize_root(root, post_probe)


async def _load_library_roots_for_rebind(
    session: AsyncSession,
    *,
    lock: bool,
) -> list[LibraryRoot]:
    statement = select(LibraryRoot).order_by(LibraryRoot.id.asc())
    if lock:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def _build_library_root_rebind_preview(
    session: AsyncSession,
    roots: Sequence[LibraryRoot],
    *,
    library_root_id: int,
    replacement_path: str,
    actor_id: int,
    issue_token: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = next((item for item in roots if item.id == library_root_id), None)
    if root is None:
        raise LibraryRootNotFoundError()

    replacement_probe = await asyncio.to_thread(
        _probe_candidate_root,
        replacement_path,
        require_write_probe=root.allow_managed_writes,
    )
    current_probe = await asyncio.to_thread(
        _probe_configured_root,
        root.path,
        require_write_probe=False,
    )
    current_display = Path(os.path.normpath(root.path))
    same_configured_path = current_display == replacement_probe.display_path
    same_physical_directory = _probes_share_identity(current_probe, replacement_probe)
    overlaps_current_path = not same_physical_directory and (
        _paths_overlap(current_display, replacement_probe.display_path)
        or (
            current_probe.resolved_path is not None
            and replacement_probe.resolved_path is not None
            and _paths_overlap(current_probe.resolved_path, replacement_probe.resolved_path)
        )
    )

    blockers = list(replacement_probe.blocking_reasons)
    blockers.extend(
        _root_conflicts(
            roots,
            name=root.name,
            probe=replacement_probe,
            exclude_root_id=root.id,
        )
    )
    blockers.extend(
        _capability_blockers(
            replacement_probe,
            allow_referenced_registrations=root.allow_referenced_registrations,
            allow_managed_writes=root.allow_managed_writes,
        )
    )
    if same_configured_path:
        blockers.append("Replacement path matches the current library root path.")
    if await _has_active_import(session):
        blockers.append(_MUTATION_BLOCKED_MESSAGE)
    blockers = _deduplicate(blockers)

    warnings = list(replacement_probe.warnings)
    if same_physical_directory and not same_configured_path:
        warnings.append("Replacement path is an alias of the current root's physical directory.")
    if overlaps_current_path:
        warnings.append("Replacement path overlaps the current root path.")
    warnings = _deduplicate(warnings)

    impact, association_digest, association_blockers = await _load_library_root_rebind_impact(
        session,
        root,
        replacement_probe=replacement_probe,
    )
    blockers.extend(association_blockers)
    blockers = _deduplicate(blockers)
    snapshot: dict[str, Any] = {
        "root_id": root.id,
        "root_updated_at": _timestamp_value(root.updated_at),
        "root_name": root.name,
        "current_path": root.path,
        "replacement_path": str(replacement_probe.display_path),
        "enabled": root.enabled,
        "allow_referenced_registrations": root.allow_referenced_registrations,
        "allow_managed_writes": root.allow_managed_writes,
        "is_default_managed_destination": root.is_default_managed_destination,
        "current_identity": _probe_identity_digest(current_probe),
        "replacement_identity": _probe_identity_digest(replacement_probe),
        "replacement_available": replacement_probe.available,
        "replacement_readable": replacement_probe.readable,
        "replacement_writable": replacement_probe.writable,
        "replacement_status": replacement_probe.status,
        "same_physical_directory": same_physical_directory,
        "overlaps_current_path": overlaps_current_path,
        "configured_roots_digest": _configured_roots_digest(roots),
        "path_associations_digest": association_digest,
        "impact": impact,
    }
    can_rebind = not blockers
    preview_token = (
        _build_rebind_preview_token(snapshot=snapshot, actor_id=actor_id)
        if can_rebind and issue_token
        else None
    )
    preview = {
        "library_root_id": root.id,
        "root_name": root.name,
        "current_path": root.path,
        "replacement_path": str(replacement_probe.display_path),
        **_capability_dict(replacement_probe),
        "warnings": warnings,
        "blocking_reasons": blockers,
        "same_physical_directory": same_physical_directory,
        "overlaps_current_path": overlaps_current_path,
        "impact": impact,
        "can_rebind": can_rebind,
        "preview_token": preview_token,
    }
    return preview, snapshot


async def _load_library_root_rebind_impact(
    session: AsyncSession,
    root: LibraryRoot,
    *,
    replacement_probe: _RootProbe,
) -> tuple[dict[str, int | bool], str, list[str]]:
    (
        library_file_count,
        library_file_blocking_count,
        library_file_digest,
    ) = await _inspect_rebind_path_scope(
        session,
        category="library_file",
        id_column=LibraryFile.id,
        path_column=LibraryFile.file_path,
        root_column=LibraryFile.library_root_id,
        root_id=root.id,
        replacement_probe=replacement_probe,
    )
    series_count = int(
        await session.scalar(select(func.count(Series.id)).where(Series.library_root_id == root.id))
        or 0
    )
    _series_path_count, series_blocking_count, series_digest = await _inspect_rebind_path_scope(
        session,
        category="series",
        id_column=Series.id,
        path_column=Series.path,
        root_column=Series.library_root_id,
        root_id=root.id,
        replacement_probe=replacement_probe,
        exclude_null_paths=True,
    )
    preferred_series_count = int(
        await session.scalar(
            select(func.count(Series.id)).where(Series.preferred_library_root_id == root.id)
        )
        or 0
    )
    (
        story_arc_placement_count,
        story_arc_placement_blocking_count,
        story_arc_placement_digest,
    ) = await _inspect_rebind_path_scope(
        session,
        category="story_arc_placement",
        id_column=StoryArcPlacement.id,
        path_column=StoryArcPlacement.placement_path,
        root_column=StoryArcPlacement.library_root_id,
        root_id=root.id,
        replacement_probe=replacement_probe,
    )
    impact: dict[str, int | bool] = {
        "library_file_count": library_file_count,
        "series_count": series_count,
        "preferred_series_count": preferred_series_count,
        "story_arc_placement_count": story_arc_placement_count,
        "library_file_blocking_count": library_file_blocking_count,
        "series_blocking_count": series_blocking_count,
        "story_arc_placement_blocking_count": story_arc_placement_blocking_count,
        "affects_default_destination": root.is_default_managed_destination,
        "affects_preferred_series": preferred_series_count > 0,
    }
    association_digest = sha256(
        (
            f"library_file:{library_file_digest}\n"
            f"series:{series_digest}\n"
            f"story_arc_placement:{story_arc_placement_digest}\n"
        ).encode()
    ).hexdigest()
    blockers: list[str] = []
    if library_file_blocking_count:
        blockers.append(
            _path_migration_required_message(
                library_file_blocking_count,
                singular="registered library file path",
                plural="registered library file paths",
            )
        )
    if series_blocking_count:
        blockers.append(
            _path_migration_required_message(
                series_blocking_count,
                singular="current series path",
                plural="current series paths",
            )
        )
    if story_arc_placement_blocking_count:
        blockers.append(
            _path_migration_required_message(
                story_arc_placement_blocking_count,
                singular="Story Arc placement path",
                plural="Story Arc placement paths",
            )
        )
    return impact, association_digest, blockers


async def _inspect_rebind_path_scope(
    session: AsyncSession,
    *,
    category: str,
    id_column: Any,
    path_column: Any,
    root_column: Any,
    root_id: int,
    replacement_probe: _RootProbe,
    exclude_null_paths: bool = False,
) -> tuple[int, int, str]:
    """Inspect one path-bearing root scope in bounded keyset pages."""
    digest = sha256()
    cursor = 0
    total_count = 0
    blocking_count = 0
    while True:
        statement = select(id_column, path_column).where(
            root_column == root_id,
            id_column > cursor,
        )
        if exclude_null_paths:
            statement = statement.where(path_column.is_not(None))
        rows = (
            await session.execute(statement.order_by(id_column.asc()).limit(_REBIND_PATH_PAGE_SIZE))
        ).all()
        if not rows:
            break
        for association_id, raw_path in rows:
            path_value = str(raw_path) if raw_path is not None else ""
            cursor = int(association_id)
            total_count += 1
            digest.update(f"{category}|{cursor}|{path_value}\n".encode())
            if not _path_is_live_inside_replacement(path_value, replacement_probe):
                blocking_count += 1
        if len(rows) < _REBIND_PATH_PAGE_SIZE:
            break
    return total_count, blocking_count, digest.hexdigest()


def _path_is_live_inside_replacement(raw_path: str, replacement_probe: _RootProbe) -> bool:
    if (
        not raw_path
        or not raw_path.isprintable()
        or "\x00" in raw_path
        or replacement_probe.resolved_path is None
    ):
        return False
    untrusted = Path(raw_path)
    if not untrusted.is_absolute() or ".." in untrusted.parts:
        return False
    candidate = Path(os.path.normpath(raw_path))
    if not (
        candidate == replacement_probe.display_path
        or candidate.is_relative_to(replacement_probe.display_path)
    ):
        return False
    try:
        # Persisted paths are not rewritten by rebind, so both their lexical
        # location and live resolved target must remain inside the replacement.
        # codeql[py/path-injection]
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (
        resolved_candidate == replacement_probe.resolved_path
        or resolved_candidate.is_relative_to(replacement_probe.resolved_path)
    )


def _path_migration_required_message(count: int, *, singular: str, plural: str) -> str:
    subject = singular if count == 1 else plural
    verb = "falls" if count == 1 else "fall"
    return (
        f"{count} {subject} {verb} outside or cannot be validated within the replacement root. "
        "An explicit path migration or repair is required before rebinding."
    )


def _rebind_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_REBIND_TOKEN_SALT)


def _build_rebind_preview_token(*, snapshot: dict[str, Any], actor_id: int) -> str:
    return str(
        _rebind_serializer().dumps(
            {
                "version": _REBIND_TOKEN_VERSION,
                "action": _REBIND_ACTION,
                "actor_id": actor_id,
                "library_root_id": snapshot["root_id"],
                "replacement_path": snapshot["replacement_path"],
                "snapshot": snapshot,
            }
        )
    )


def _load_rebind_preview_token(token: str) -> dict[str, object]:
    try:
        payload = _rebind_serializer().loads(token, max_age=_REBIND_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The library root rebind preview expired. Preview it again.") from exc
    except BadSignature as exc:
        raise ValidationError(
            "The library root rebind preview is invalid. Preview it again."
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationError("The library root rebind preview is invalid. Preview it again.")
    return payload


def _validate_rebind_token_scope(
    payload: dict[str, object],
    *,
    library_root_id: int,
    replacement_path: str,
    actor_id: int,
) -> dict[str, Any]:
    snapshot = payload.get("snapshot")
    if (
        payload.get("version") != _REBIND_TOKEN_VERSION
        or payload.get("action") != _REBIND_ACTION
        or payload.get("actor_id") != actor_id
        or payload.get("library_root_id") != library_root_id
        or payload.get("replacement_path") != replacement_path
        or not isinstance(snapshot, dict)
    ):
        raise ValidationError("The library root rebind preview does not match this request.")
    return snapshot


def _timestamp_value(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat(timespec="microseconds")) if callable(isoformat) else ""


def _probe_identity_digest(probe: _RootProbe) -> str | None:
    if probe.resolved_path is None:
        return None
    identity = probe.identity or (0, 0)
    payload = f"{probe.resolved_path}\0{identity[0]}\0{identity[1]}".encode()
    return sha256(payload).hexdigest()


def _probes_share_identity(first: _RootProbe, second: _RootProbe) -> bool:
    if first.identity is not None and second.identity is not None:
        return first.identity == second.identity
    return (
        first.resolved_path is not None
        and second.resolved_path is not None
        and first.resolved_path == second.resolved_path
    )


def _configured_roots_digest(roots: Sequence[LibraryRoot]) -> str:
    payload = [
        {
            "id": root.id,
            "name": root.name,
            "path": root.path,
            "updated_at": _timestamp_value(root.updated_at),
        }
        for root in roots
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


async def _has_active_import(session: AsyncSession) -> bool:
    active_job_id = await session.scalar(
        select(ImportJob.id).where(ImportJob.status.in_(tuple(ACTIVE_IMPORT_JOB_STATUSES))).limit(1)
    )
    return active_job_id is not None


async def _assert_no_active_import(session: AsyncSession) -> None:
    if await _has_active_import(session):
        raise ValidationError(_MUTATION_BLOCKED_MESSAGE)


async def _clear_default(session: AsyncSession) -> None:
    await session.execute(
        update(LibraryRoot)
        .where(LibraryRoot.is_default_managed_destination.is_(True))
        .values(is_default_managed_destination=False)
    )
    await session.flush()


async def _sync_legacy_default(session: AsyncSession, path: str) -> None:
    config = await session.get(SystemConfig, "comics_directory")
    if config is None:
        config = SystemConfig(
            key="comics_directory",
            value=path,
            value_type="string",
            description="Default managed library destination.",
        )
        session.add(config)
    else:
        config.value = path
        config.value_type = "string"
    await session.flush()


def _normalize_name(name: str) -> str:
    normalized = name.strip()
    if not normalized or len(normalized) > 255 or not normalized.isprintable():
        raise ValidationError("Library root name must be printable and 1-255 characters long.")
    return normalized


def _validate_roles(
    *,
    allow_referenced_registrations: bool,
    allow_managed_writes: bool,
    is_default_managed_destination: bool,
) -> None:
    if not allow_referenced_registrations and not allow_managed_writes:
        raise ValidationError("A library root must allow at least one role.")
    if is_default_managed_destination and not allow_managed_writes:
        raise ValidationError("The default managed destination must allow managed writes.")


def _normalize_candidate_path(raw_path: str) -> Path:
    if not raw_path or len(raw_path) > 1000 or not raw_path.isprintable() or "\x00" in raw_path:
        raise ValidationError("Library root path contains unsafe characters.")
    untrusted = Path(raw_path)
    if not untrusted.is_absolute():
        raise ValidationError("Library root path must be an absolute container-visible path.")
    if ".." in untrusted.parts:
        raise ValidationError("Library root path cannot contain traversal components.")
    display_path = Path(os.path.normpath(raw_path))
    if display_path == Path("/"):
        raise ValidationError("The filesystem root cannot be configured as a library root.")
    return display_path


def _probe_candidate_root(raw_path: str, *, require_write_probe: bool) -> _RootProbe:
    display_path = _normalize_candidate_path(raw_path)
    return _probe_path(
        display_path,
        require_write_probe=require_write_probe,
        reject_sensitive=True,
    )


def _probe_configured_root(raw_path: str, *, require_write_probe: bool) -> _RootProbe:
    try:
        display_path = _normalize_candidate_path(raw_path)
    except ValidationError as exc:
        return _unavailable_probe(Path(raw_path), exc.message)
    return _probe_path(
        display_path,
        require_write_probe=require_write_probe,
        reject_sensitive=False,
    )


def _probe_path(
    display_path: Path,
    *,
    require_write_probe: bool,
    reject_sensitive: bool,
) -> _RootProbe:
    try:
        # The authenticated root workflow already requires a bounded absolute
        # path without traversal; this strict resolution is the safety probe.
        # codeql[py/path-injection]
        resolved = display_path.resolve(strict=True)
    except (OSError, RuntimeError):
        return _unavailable_probe(display_path, "Library root path must be an existing directory.")
    if not resolved.is_dir():
        return _unavailable_probe(display_path, "Library root path must be an existing directory.")
    if is_sensitive_path(resolved):
        message = "Sensitive system directories cannot be configured as library roots."
        if reject_sensitive:
            raise ValidationError(message)
        return _unavailable_probe(display_path, message)

    readable = _can_read_and_traverse(resolved)
    writable = _can_write(resolved, create_probe=require_write_probe)
    try:
        usage = shutil.disk_usage(resolved)
        free_bytes: int | None = usage.free
    except OSError:
        free_bytes = None
    try:
        stat_result = resolved.stat()
        identity = _filesystem_identity(stat_result.st_dev, stat_result.st_ino)
    except OSError:
        identity = None

    warnings: list[str] = []
    if not readable:
        warnings.append("Directory is not readable and traversable by Pullbox.")
    elif not writable:
        warnings.append("Directory is read-only to Pullbox.")
    if free_bytes is not None and free_bytes < _LOW_CAPACITY_BYTES:
        warnings.append("Directory has less than 1 GiB of free space.")
    if not readable:
        status = "unavailable"
    elif not writable:
        status = "read_only"
    elif free_bytes is not None and free_bytes < _LOW_CAPACITY_BYTES:
        status = "low_capacity"
    else:
        status = "ready"
    return _RootProbe(
        display_path=display_path,
        resolved_path=resolved,
        identity=identity,
        available=True,
        readable=readable,
        writable=writable,
        free_bytes=free_bytes,
        status=status,
        warnings=tuple(warnings),
        blocking_reasons=(),
    )


def _unavailable_probe(display_path: Path, reason: str) -> _RootProbe:
    return _RootProbe(
        display_path=display_path,
        resolved_path=None,
        identity=None,
        available=False,
        readable=False,
        writable=False,
        free_bytes=None,
        status="unavailable",
        warnings=(reason,),
        blocking_reasons=(reason,),
    )


def _can_read_and_traverse(path: Path) -> bool:
    if not os.access(path, os.R_OK | os.X_OK):
        return False
    try:
        # Root validation intentionally opens only the selected directory and
        # returns no entry names or contents.
        # codeql[py/path-injection]
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


def _can_write(path: Path, *, create_probe: bool) -> bool:
    if not os.access(path, os.W_OK | os.X_OK):
        return False
    if not create_probe:
        return True
    descriptor: int | None = None
    probe_path: str | None = None
    try:
        # The directory passed the root-path policy; mkstemp owns the random
        # leaf name and the probe is removed before this call returns.
        # codeql[py/path-injection]
        descriptor, probe_path = tempfile.mkstemp(prefix=".pullbox-root-probe-", dir=path)
        os.close(descriptor)
        descriptor = None
        os.unlink(probe_path)
        probe_path = None
    except OSError:
        return False
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if probe_path is not None:
            with suppress(OSError):
                os.unlink(probe_path)
    return True


def _capability_blockers(
    probe: _RootProbe,
    *,
    allow_referenced_registrations: bool,
    allow_managed_writes: bool,
) -> list[str]:
    blockers: list[str] = []
    if allow_referenced_registrations and not probe.readable:
        blockers.append("Referenced registrations require a readable, traversable directory.")
    if allow_managed_writes and (not probe.readable or not probe.writable):
        blockers.append("Managed writes require a readable, traversable, writable directory.")
    return blockers


def _root_conflicts(
    roots: Sequence[LibraryRoot],
    *,
    name: str,
    probe: _RootProbe,
    exclude_root_id: int | None,
) -> list[str]:
    conflicts: list[str] = []
    for root in roots:
        if root.id == exclude_root_id:
            continue
        if root.name.strip().casefold() == name.casefold():
            conflicts.append("Library root names must be unique, ignoring case.")

        existing_display = Path(os.path.normpath(root.path))
        if existing_display == probe.display_path:
            conflicts.append("This library root path is already configured.")
            continue
        existing_resolved: Path | None = None
        existing_identity: tuple[int, int] | None = None
        try:
            existing_resolved = existing_display.resolve(strict=True)
            stat_result = existing_resolved.stat()
            existing_identity = _filesystem_identity(stat_result.st_dev, stat_result.st_ino)
        except (OSError, RuntimeError):
            pass
        if (
            probe.identity is not None
            and existing_identity is not None
            and probe.identity == existing_identity
        ):
            conflicts.append("This path resolves to the same physical directory as another root.")
            continue
        if _paths_overlap(probe.display_path, existing_display) or (
            probe.resolved_path is not None
            and existing_resolved is not None
            and _paths_overlap(probe.resolved_path, existing_resolved)
        ):
            conflicts.append("This library root overlaps or is nested inside another root.")
    return _deduplicate(conflicts)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _filesystem_identity(device: int, inode: int) -> tuple[int, int] | None:
    """Return useful identity evidence without conflating zero-inode network mounts."""
    return (device, inode) if inode else None


def _deduplicate(messages: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _raise_blockers(blockers: Sequence[str]) -> None:
    unique = _deduplicate(blockers)
    if unique:
        raise ValidationError(unique[0], details={"blocking_reasons": unique})


def _capability_dict(probe: _RootProbe) -> dict[str, Any]:
    return {
        "available": probe.available,
        "readable": probe.readable,
        "writable": probe.writable,
        "free_bytes": probe.free_bytes,
        "status": probe.status,
        "warnings": list(probe.warnings),
    }


def _serialize_root(
    root: LibraryRoot,
    probe: _RootProbe,
    *,
    extra_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    warnings = list(probe.warnings)
    warnings.extend(extra_warnings)
    if not root.enabled:
        warnings.append("Library root is disabled.")
    return {
        "id": root.id,
        "name": root.name,
        "path": root.path,
        "enabled": root.enabled,
        "allow_referenced_registrations": root.allow_referenced_registrations,
        "allow_managed_writes": root.allow_managed_writes,
        "is_default_managed_destination": root.is_default_managed_destination,
        **_capability_dict(probe),
        "warnings": _deduplicate(warnings),
        "can_disable": not root.is_default_managed_destination,
    }
