"""Library API routes — unmatched files, browser actions, manual match, stats."""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from pullbox.api.deps import (  # noqa: TC001
    AuthenticatedUser,
    DbSession,
    InteractiveOperatorUser,
)
from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.models.config import SystemConfig
from pullbox.models.library import (
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.schemas.library import (
    LibraryBrowserActionFlags,
    LibraryBrowserConvertRequest,
    LibraryBrowserDeleteContext,
    LibraryBrowserDeleteRequest,
    LibraryBrowserEntryResponse,
    LibraryBrowserManualRenameRequest,
    LibraryBrowserManualRenameValidationResponse,
    LibraryBrowserRenameContext,
    LibraryBrowserStorageSummary,
    LibraryFileResponse,
    LibraryStats,
    ManualMatchRequest,
)
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.services.library_convert_service import convert_library_file
from pullbox.services.library_delete_service import (
    LibraryDeleteContext,
    build_delete_context,
    delete_library_entry,
)
from pullbox.services.library_rename_service import rename_library_entry
from pullbox.utilities.settings import resolve_trash_directory, resolve_utility_directory

router = APIRouter(prefix="/library", tags=["library"], include_in_schema=False)

_CONVERTIBLE_FILE_FORMATS = frozenset({"cbr", "cb7", "pdf"})
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_LIBRARY_NAME_LENGTH = 255


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _sanitize_library_name(name: str) -> str:
    sanitized = _ILLEGAL_NAME_CHARS.sub("", name)
    sanitized = re.sub(r"\s{2,}", " ", sanitized).strip()
    sanitized = sanitized.rstrip(". ")
    return sanitized


def _enabled_root_path(root: LibraryRoot) -> Path:
    return Path(root.path).expanduser().resolve()


def _normalize_library_path(path_value: str | Path | None) -> str | None:
    if path_value is None:
        return None
    return str(Path(path_value).expanduser().resolve(strict=False)).rstrip("/")


async def _load_enabled_library_roots(session: DbSession) -> list[LibraryRoot]:
    result = await session.execute(
        select(LibraryRoot).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id)
    )
    return list(result.scalars().all())


def _resolve_library_target(
    path_value: str,
    *,
    roots: list[LibraryRoot],
    require_exists: bool = True,
) -> tuple[Path, LibraryRoot]:
    raw_path = (path_value or "").strip()
    if not raw_path:
        raise ValidationError("A library path is required.")

    root_map = {root.id: _enabled_root_path(root) for root in roots}
    try:
        resolved = resolve_path_inside_roots(
            raw_path,
            root_map.values(),
            require_exists=require_exists,
        )
    except ValueError as exc:
        message = str(exc)
        if "does not exist" in message:
            raise ValidationError("Selected library item no longer exists on disk.") from None
        raise ValidationError("Selected path is outside the enabled library roots.") from None

    matching_root = next(
        (
            root
            for root in roots
            if (root_path := root_map[root.id]) == resolved or _is_relative_to(resolved, root_path)
        ),
        None,
    )
    if matching_root is None:
        raise ValidationError("Selected path is outside the enabled library roots.")

    return resolved, matching_root


def _library_entry_kind(path: Path, *, root_path: Path) -> str:
    if path == root_path:
        return "root"
    if path.is_dir():
        return "folder"
    return "file"


def _library_file_format(path: Path) -> str | None:
    suffix = path.suffix.lower().lstrip(".")
    return suffix.upper() if suffix else None


def _visible_child_count(path: Path) -> int | None:
    if not path.is_dir():
        return None
    try:
        with os.scandir(path) as entries:
            return sum(1 for entry in entries if not entry.name.startswith("."))
    except OSError:
        return None


def _entry_size_bytes(path: Path) -> int | None:
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return None

    if not path.is_dir():
        return None

    total = 0
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            try:
                total += int(Path(root, filename).stat().st_size)
            except OSError:
                continue
    return total


def _entry_modified_at(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _entry_permissions_label(path: Path) -> str | None:
    try:
        return stat.filemode(path.stat().st_mode)
    except OSError:
        return None


def _build_library_actions(
    *,
    kind: str,
    file_format: str | None,
    tracking_scope: str = "tracked_file",
    referenced_file_count: int = 0,
) -> LibraryBrowserActionFlags:
    normalized_format = (file_format or "").strip().lower()
    if kind == "root":
        return LibraryBrowserActionFlags(can_properties=True)

    if tracking_scope == "tracked_descendant_folder":
        return LibraryBrowserActionFlags(can_properties=True)

    if tracking_scope not in {
        "tracked_file",
        "tracked_series_folder",
        "tracked_file_parent_folder",
    }:
        return LibraryBrowserActionFlags(can_properties=False)

    if referenced_file_count:
        return LibraryBrowserActionFlags(can_properties=True, can_delete=True)

    return LibraryBrowserActionFlags(
        can_properties=True,
        can_rename=True,
        can_auto_rename=True,
        can_convert=kind == "file" and normalized_format in _CONVERTIBLE_FILE_FORMATS,
        can_delete=True,
    )


async def _tracked_library_file_exists(session: DbSession, target: Path) -> bool:
    target_str = str(target).rstrip("/")
    if (
        await session.execute(select(LibraryFile.id).where(LibraryFile.file_path == target_str))
    ).scalar_one_or_none() is not None:
        return True

    normalized_target = _normalize_library_path(target)
    for stored_path in (
        await session.execute(
            select(LibraryFile.file_path).where(LibraryFile.file_name == target.name)
        )
    ).scalars():
        if _normalize_library_path(stored_path) == normalized_target:
            return True
    return False


async def _folder_is_direct_tracked_file_parent(session: DbSession, target: Path) -> bool:
    normalized_target = _normalize_library_path(target)
    if normalized_target is None:
        return False

    for stored_path in (await session.execute(select(LibraryFile.file_path))).scalars():
        normalized_path = _normalize_library_path(stored_path)
        if normalized_path is not None and str(Path(normalized_path).parent) == normalized_target:
            return True
    return False


async def _folder_has_tracked_descendants(session: DbSession, target: Path) -> bool:
    prefix = str(target).rstrip("/")
    file_count = int(
        (
            await session.execute(
                select(func.count(LibraryFile.id)).where(LibraryFile.file_path.like(f"{prefix}/%"))
            )
        ).scalar_one()
        or 0
    )
    if file_count > 0:
        return True

    normalized_target = _normalize_library_path(target)
    if normalized_target is not None:
        normalized_prefix = f"{normalized_target}/"
        for stored_path in (await session.execute(select(LibraryFile.file_path))).scalars():
            normalized_path = _normalize_library_path(stored_path)
            if normalized_path is not None and normalized_path.startswith(normalized_prefix):
                return True

    series_count = int(
        (
            await session.execute(
                select(func.count(Series.id)).where(Series.path.like(f"{prefix}/%"))
            )
        ).scalar_one()
        or 0
    )
    if series_count > 0:
        return True

    if normalized_target is not None:
        normalized_prefix = f"{normalized_target}/"
        for series_stored_path in (
            await session.execute(select(Series.path).where(Series.path.is_not(None)))
        ).scalars():
            normalized_path = _normalize_library_path(series_stored_path)
            if normalized_path is not None and normalized_path.startswith(normalized_prefix):
                return True

    return series_count > 0


async def _library_browser_tracking_scope(
    session: DbSession,
    *,
    target: Path,
    kind: str,
    delete_context: LibraryDeleteContext | None = None,
) -> str:
    """Return how strongly the Library catalog owns this browser path."""
    if kind == "root":
        return "root"
    if kind == "file":
        return (
            "tracked_file" if await _tracked_library_file_exists(session, target) else "untracked"
        )
    if delete_context is not None and delete_context.mode == "series":
        return "tracked_series_folder"

    normalized_target = _normalize_library_path(target)
    for series_path in (
        await session.execute(select(Series.path).where(Series.path.is_not(None)))
    ).scalars():
        if _normalize_library_path(series_path) == normalized_target:
            return "tracked_series_folder"

    if await _folder_is_direct_tracked_file_parent(session, target):
        return "tracked_file_parent_folder"

    if await _folder_has_tracked_descendants(session, target):
        return "tracked_descendant_folder"
    return "untracked"


def _require_library_catalog_entry(tracking_scope: str) -> None:
    if tracking_scope == "untracked":
        raise ValidationError("This path is not tracked by Pullbox's library catalog.")


def _require_mutable_library_catalog_entry(tracking_scope: str) -> None:
    _require_library_catalog_entry(tracking_scope)
    if tracking_scope == "tracked_descendant_folder":
        raise ValidationError(
            "Only tracked series folders, tracked file folders, and tracked files "
            "can be changed from Library."
        )


def _kind_label(kind: str) -> str:
    if kind == "root":
        return "Library Root"
    if kind == "folder":
        return "Folder"
    return "File"


def _root_storage_summary(root_path: Path) -> LibraryBrowserStorageSummary:
    try:
        usage = shutil.disk_usage(root_path)
    except OSError:
        return LibraryBrowserStorageSummary()

    total_bytes = int(usage.total)
    used_bytes = int(usage.used)
    free_bytes = int(usage.free)
    used_pct = (used_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
    return LibraryBrowserStorageSummary(
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        used_pct=used_pct,
    )


async def _build_library_rename_context(
    session: DbSession,
    *,
    target: Path,
    kind: str,
    delete_context: LibraryDeleteContext,
) -> LibraryBrowserRenameContext:
    """Detect stale DB-reference cases that should block library renames."""
    if kind == "root":
        return LibraryBrowserRenameContext(
            stale_reference=True,
            reason_code="root",
            message="Library roots cannot be renamed from the browser.",
            db_check_url="/utilities/db-check",
        )

    target_str = str(target).rstrip("/")
    normalized_target = _normalize_library_path(target)

    if kind == "folder":
        exact_series = None
        for series_id, series_path in (
            await session.execute(select(Series.id, Series.path).where(Series.path.is_not(None)))
        ).all():
            if _normalize_library_path(series_path) == normalized_target:
                exact_series = series_id
                break
        if delete_context.mode == "series" and exact_series is None:
            return LibraryBrowserRenameContext(
                stale_reference=True,
                reason_code="stale_series_path",
                message=(
                    "This folder is associated with a series in Pullbox, but the stored "
                    "series path does not match the folder on disk. Run the Database "
                    "Integrity Check before renaming it from Library."
                ),
                db_check_url="/utilities/db-check",
            )
        return LibraryBrowserRenameContext(db_check_url="/utilities/db-check")

    exact_file = (
        await session.execute(select(LibraryFile.id).where(LibraryFile.file_path == target_str))
    ).scalar_one_or_none()
    if exact_file is None:
        for file_id, file_path in (
            await session.execute(
                select(LibraryFile.id, LibraryFile.file_path).where(
                    LibraryFile.file_name == target.name
                )
            )
        ).all():
            if _normalize_library_path(file_path) == normalized_target:
                exact_file = file_id
                break
    if exact_file is not None:
        return LibraryBrowserRenameContext(db_check_url="/utilities/db-check")

    parent = target.parent
    parent_delete_context = await build_delete_context(
        session,
        target=parent,
        kind="folder",
        trash_enabled=delete_context.trash_enabled,
    )
    exact_parent_series = None
    normalized_parent = _normalize_library_path(parent)
    for series_id, series_path in (
        await session.execute(select(Series.id, Series.path).where(Series.path.is_not(None)))
    ).all():
        if _normalize_library_path(series_path) == normalized_parent:
            exact_parent_series = series_id
            break
    if parent_delete_context.mode == "series" and exact_parent_series is None:
        return LibraryBrowserRenameContext(
            stale_reference=True,
            reason_code="stale_series_path",
            message=(
                "This file lives inside a series folder with a stale database path. "
                "Run the Database Integrity Check before renaming it from Library."
            ),
            db_check_url="/utilities/db-check",
        )

    return LibraryBrowserRenameContext(db_check_url="/utilities/db-check")


async def _load_configured_utility_trash_dir(session: DbSession) -> Path | None:
    result = await session.execute(
        select(SystemConfig).where(SystemConfig.key.in_(["utility_trash_folder"]))
    )
    configs = {cfg.key: cfg.value for cfg in result.scalars().all()}
    settings = get_settings()
    return resolve_trash_directory(
        trash_folder=configs.get("utility_trash_folder", ""),
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )


async def _load_library_convert_trash_dir(session: DbSession) -> Path:
    result = await session.execute(
        select(SystemConfig).where(SystemConfig.key.in_(["utility_trash_folder"]))
    )
    configs = {cfg.key: cfg.value for cfg in result.scalars().all()}
    settings = get_settings()
    return resolve_utility_directory(
        db_value=configs.get("utility_trash_folder", ""),
        default_parent=settings.library_root,
        default_subdir=".trash",
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )


async def _validate_library_browser_convert(
    session: DbSession,
    *,
    body: LibraryBrowserConvertRequest,
) -> tuple[Path, LibraryRoot]:
    roots = await _load_enabled_library_roots(session)
    source, root = _resolve_library_target(body.path, roots=roots)
    root_path = _enabled_root_path(root)
    kind = _library_entry_kind(source, root_path=root_path)
    if kind != "file":
        raise ValidationError("Only files can be converted from the browser.")
    if not await _tracked_library_file_exists(session, source):
        raise ValidationError("This path is not tracked by Pullbox's library catalog.")
    storage_mode = (
        await session.execute(
            select(LibraryFile.storage_mode).where(LibraryFile.file_path == str(source))
        )
    ).scalar_one_or_none()
    if storage_mode == LibraryFileStorageMode.REFERENCED:
        raise ValidationError(
            "Referenced library files cannot be converted. They must stay unchanged on disk."
        )

    source_format = source.suffix.lower().lstrip(".")
    if source_format not in _CONVERTIBLE_FILE_FORMATS:
        raise ValidationError("This file format cannot be converted to CBZ from the browser.")

    target_path = source.with_suffix(".cbz")
    if target_path.exists():
        raise ValidationError("A CBZ file with that name already exists.")

    return source, root


# ── Unmatched Files ──────────────────────────────────────────────────


@router.get("/unmatched", response_model=PaginatedResponse[LibraryFileResponse])
async def list_unmatched(
    _user: AuthenticatedUser,
    session: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PaginatedResponse[LibraryFileResponse]:
    """List library files that haven't been matched to an issue."""
    from pullbox.services.library_service import LibraryService

    total = (
        await session.execute(
            select(func.count(LibraryFile.id)).where(
                LibraryFile.match_confidence == MatchConfidence.UNMATCHED
            )
        )
    ).scalar_one()

    files = await LibraryService.get_unmatched(session, limit, offset)
    items = [LibraryFileResponse.model_validate(f) for f in files]

    return PaginatedResponse[LibraryFileResponse](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


# ── Library Browser ──────────────────────────────────────────────────


@router.get("/browser/entry", response_model=LibraryBrowserEntryResponse)
async def library_browser_entry(
    _user: InteractiveOperatorUser,
    session: DbSession,
    path: str = Query(..., description="Absolute library path"),
) -> LibraryBrowserEntryResponse:
    """Return single-entry metadata for the Library context-menu modals."""
    roots = await _load_enabled_library_roots(session)
    resolved, root = _resolve_library_target(path, roots=roots)
    root_path = _enabled_root_path(root)
    kind = _library_entry_kind(resolved, root_path=root_path)
    file_format = _library_file_format(resolved)
    trash_dir = await _load_configured_utility_trash_dir(session)
    delete_context = await build_delete_context(
        session,
        target=resolved,
        kind=kind,
        trash_enabled=trash_dir is not None,
    )
    tracking_scope = await _library_browser_tracking_scope(
        session,
        target=resolved,
        kind=kind,
        delete_context=delete_context,
    )
    _require_library_catalog_entry(tracking_scope)
    rename_context = await _build_library_rename_context(
        session,
        target=resolved,
        kind=kind,
        delete_context=delete_context,
    )

    return LibraryBrowserEntryResponse(
        name=resolved.name or str(resolved),
        path=str(resolved),
        kind=kind,
        kind_label=_kind_label(kind),
        root_name=root.name,
        root_path=str(root_path),
        file_format=file_format,
        size_bytes=_entry_size_bytes(resolved),
        item_count=_visible_child_count(resolved),
        modified_at=_entry_modified_at(resolved),
        permissions_label=_entry_permissions_label(resolved),
        actions=_build_library_actions(
            kind=kind,
            file_format=file_format,
            tracking_scope=tracking_scope,
            referenced_file_count=delete_context.referenced_file_count,
        ),
        delete_context=LibraryBrowserDeleteContext.model_validate(asdict(delete_context)),
        rename_context=rename_context,
        storage=_root_storage_summary(root_path),
    )


async def _validate_library_browser_rename(
    session: DbSession,
    *,
    body: LibraryBrowserManualRenameRequest,
) -> LibraryBrowserManualRenameValidationResponse:
    """Validate a single Library browser rename request."""
    roots = await _load_enabled_library_roots(session)
    source, root = _resolve_library_target(body.path, roots=roots)
    root_path = _enabled_root_path(root)
    kind = _library_entry_kind(source, root_path=root_path)
    trash_dir = await _load_configured_utility_trash_dir(session)
    delete_context = await build_delete_context(
        session,
        target=source,
        kind=kind,
        trash_enabled=trash_dir is not None,
    )
    tracking_scope = await _library_browser_tracking_scope(
        session,
        target=source,
        kind=kind,
        delete_context=delete_context,
    )
    rename_context = await _build_library_rename_context(
        session,
        target=source,
        kind=kind,
        delete_context=delete_context,
    )

    if kind == "root":
        raise ValidationError("Library roots cannot be renamed from the browser.")
    _require_mutable_library_catalog_entry(tracking_scope)
    if delete_context.referenced_file_count:
        raise ValidationError(
            "Referenced library files cannot be renamed. They must stay unchanged on disk."
        )
    if rename_context.stale_reference:
        raise ValidationError(
            rename_context.message
            or (
                "This item has a stale database reference. "
                "Run the Database Integrity Check before renaming it from Library."
            )
        )

    proposed_name = _sanitize_library_name(body.proposed_name)
    if not proposed_name:
        raise ValidationError("Enter a valid file or folder name.")
    if len(proposed_name) > _MAX_LIBRARY_NAME_LENGTH:
        raise ValidationError(
            "Name exceeds filesystem limit "
            f"({len(proposed_name)} > {_MAX_LIBRARY_NAME_LENGTH} chars)."
        )

    if kind == "file":
        current_suffix = source.suffix.lower()
        proposed_suffix = Path(proposed_name).suffix.lower()
        if current_suffix and proposed_suffix != current_suffix:
            raise ValidationError("File rename must keep the existing extension.")

    if source.name == proposed_name:
        raise ValidationError("Name is unchanged.")

    target_path = source.parent / proposed_name
    case_only = source.name.lower() == proposed_name.lower()
    if not case_only and target_path.exists():
        raise ValidationError("A file or folder with that name already exists.")

    return LibraryBrowserManualRenameValidationResponse(
        path=str(source),
        current_name=source.name,
        proposed_name=proposed_name,
        target_path=str(target_path),
        kind=kind,
    )


@router.post(
    "/browser/rename/manual/validate",
    response_model=LibraryBrowserManualRenameValidationResponse,
)
async def validate_library_manual_rename(
    body: LibraryBrowserManualRenameRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryBrowserManualRenameValidationResponse:
    """Validate a single manual rename request from the Library browser."""
    return await _validate_library_browser_rename(session, body=body)


@router.post("/browser/rename")
async def rename_library_browser_entry(
    body: LibraryBrowserManualRenameRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Rename a single Library browser target immediately."""
    validation = await _validate_library_browser_rename(session, body=body)
    outcome = await rename_library_entry(
        session,
        source=Path(validation.path),
        target=Path(validation.target_path),
        kind=validation.kind,
    )

    return {
        "status": "ok",
        "kind": outcome.kind,
        "source_path": outcome.source_path,
        "target_path": outcome.target_path,
    }


@router.post("/browser/delete")
async def delete_library_browser_entry(
    body: LibraryBrowserDeleteRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Delete a single library browser target, optionally routing it through trash."""
    roots = await _load_enabled_library_roots(session)
    target, root = _resolve_library_target(body.path, roots=roots)
    root_path = _enabled_root_path(root)
    kind = _library_entry_kind(target, root_path=root_path)

    if kind == "root":
        raise ValidationError("Library roots cannot be deleted from the browser.")

    trash_dir = await _load_configured_utility_trash_dir(session)
    if trash_dir is not None:
        trash_dir.mkdir(parents=True, exist_ok=True)

    delete_context = await build_delete_context(
        session,
        target=target,
        kind=kind,
        trash_enabled=trash_dir is not None,
    )
    tracking_scope = await _library_browser_tracking_scope(
        session,
        target=target,
        kind=kind,
        delete_context=delete_context,
    )
    _require_mutable_library_catalog_entry(tracking_scope)
    outcome = await delete_library_entry(
        session,
        target=target,
        root=root,
        kind=kind,
        delete_context=delete_context,
        delete_files=body.delete_files,
        delete_folder=body.delete_folder,
        trash_dir=trash_dir,
    )

    return {
        "status": "ok",
        "kind": outcome.kind,
        "mode": outcome.mode,
        "source_path": outcome.source_path,
        "deleted_via_trash": outcome.deleted_via_trash,
        "result_path": outcome.result_path,
        "managed_files_deleted": outcome.managed_files_deleted,
        "referenced_files_detached": outcome.referenced_files_detached,
    }


@router.post("/browser/convert")
async def convert_library_browser_entry(
    body: LibraryBrowserConvertRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Convert a single Library browser file to CBZ immediately."""
    source, root = await _validate_library_browser_convert(session, body=body)
    trash_dir = await _load_library_convert_trash_dir(session)
    trash_dir.mkdir(parents=True, exist_ok=True)
    root_path = _enabled_root_path(root)

    if source == root_path or not _is_relative_to(source, root_path):
        trash_relative_path = source.name
    else:
        trash_relative_path = str(Path(root.name) / source.relative_to(root_path))

    outcome = await convert_library_file(
        session,
        source=source,
        trash_dir=trash_dir,
        trash_relative_path=trash_relative_path,
    )

    return {
        "status": "ok",
        "kind": outcome.kind,
        "source_path": outcome.source_path,
        "target_path": outcome.target_path,
        "original_trash_path": outcome.original_trash_path,
    }


# ── Manual Match ─────────────────────────────────────────────────────


@router.post("/match", response_model=LibraryFileResponse)
async def manual_match(
    body: ManualMatchRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> LibraryFileResponse:
    """Manually match a library file to a specific issue."""
    from pullbox.composition.services import build_matching_service

    matching_svc = build_matching_service()
    library_file = await matching_svc.manual_match(session, body.library_file_id, body.issue_id)
    return LibraryFileResponse.model_validate(library_file)


# ── Stats ────────────────────────────────────────────────────────────


@router.get("/stats", response_model=LibraryStats)
async def library_stats(
    _user: AuthenticatedUser,
    session: DbSession,
) -> LibraryStats:
    """Get library statistics overview."""
    from pullbox.services.library_service import LibraryService

    stats = await LibraryService.get_stats(session)

    roots_count = (await session.execute(select(func.count(LibraryRoot.id)))).scalar_one()

    return LibraryStats(
        total_files=stats["total_files"],
        matched_files=stats["matched"],
        unmatched_files=stats["unmatched"],
        total_size_bytes=stats["total_size_bytes"],
        roots_count=roots_count,
        format_counts={
            k: v
            for k, v in stats.items()
            if k not in ("total_files", "matched", "unmatched", "total_size_bytes")
        },
    )
