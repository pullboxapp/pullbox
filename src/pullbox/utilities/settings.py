"""Utility settings — validation, path resolution, and directory setup.

Provides validation for utility-specific system config keys,
path resolution for trash/export directories, and startup
directory creation.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def validate_utility_setting(key: str, value: str) -> None:
    """Validate a utility setting value, raising ValidationError if invalid.

    Non-utility keys are silently ignored (no-op).

    Args:
        key: The system config key name.
        value: The proposed new value as a string.
    """
    from pullbox.core.exceptions import ValidationError

    if key == "utility_worker_count":
        try:
            count = int(value)
        except ValueError:
            raise ValidationError(f"Worker count must be an integer, got: {value}") from None
        if count < 1 or count > 16:
            raise ValidationError(f"Worker count must be between 1 and 16, got: {count}")

    elif key == "utility_job_retention_days":
        try:
            days = int(value)
        except ValueError:
            raise ValidationError(f"Retention days must be an integer, got: {value}") from None
        if days < 1 or days > 365:
            raise ValidationError(f"Retention days must be between 1 and 365, got: {days}")

    elif key == "utility_trash_retention_days":
        try:
            days = int(value)
        except ValueError:
            raise ValidationError(
                f"Trash retention days must be an integer, got: {value}"
            ) from None
        if days < 1 or days > 365:
            raise ValidationError(f"Trash retention days must be between 1 and 365, got: {days}")

    elif key == "utility_log_level" and value.upper() not in _VALID_LOG_LEVELS:
        raise ValidationError(
            f"Utility log level must be one of: DEBUG, INFO, WARNING, ERROR; got: {value}"
        )


def resolve_utility_directory(
    db_value: str,
    default_parent: Path,
    default_subdir: str,
    *,
    library_root: Path | None = None,
    data_dir: Path | None = None,
) -> Path:
    """Resolve a utility directory path from a DB value or computed default.

    Supports placeholder expansion:
        {library} → library_root or default_parent
        {data}    → data_dir or default_parent

    Args:
        db_value: The value stored in system_config (empty string = use default).
        default_parent: Parent directory for the default path.
        default_subdir: Subdirectory name appended to default_parent.
        library_root: Library root path for {library} placeholder.
        data_dir: Data directory path for {data} placeholder.

    Returns:
        Resolved Path — either the custom path or the computed default.
    """
    value = db_value.strip()
    if not value:
        return default_parent / default_subdir

    # Expand placeholders
    lib = str(library_root) if library_root else str(default_parent)
    data = str(data_dir) if data_dir else str(default_parent)
    value = value.replace("{library}", lib)
    value = value.replace("{data}", data)

    return Path(value)


def resolve_export_directory(
    export_folder: str | None,
    *,
    library_root: Path | None = None,
    data_dir: Path | None = None,
) -> Path:
    """Resolve an export directory from a job/runtime config value.

    Blank values fall back to the configured utility export default.
    Placeholder values such as ``{data}`` and ``{library}`` are expanded.
    """
    if library_root is None or data_dir is None:
        from pullbox.config import get_settings

        settings = get_settings()
        library_root = library_root or settings.library_root
        data_dir = data_dir or settings.data_dir

    return resolve_utility_directory(
        db_value=(export_folder or "").strip(),
        default_parent=data_dir,
        default_subdir="exports",
        library_root=library_root,
        data_dir=data_dir,
    )


def resolve_trash_directory(
    trash_folder: str | None,
    *,
    library_root: Path | None = None,
    data_dir: Path | None = None,
) -> Path | None:
    """Resolve an optional trash directory from a job/runtime config value.

    Blank values keep the trash directory disabled for workflows where an empty
    field means "do not move originals". Placeholder values such as ``{library}``
    and ``{data}`` are expanded when a value is provided.
    """
    value = (trash_folder or "").strip()
    if not value:
        return None

    if library_root is None or data_dir is None:
        from pullbox.config import get_settings

        settings = get_settings()
        library_root = library_root or settings.library_root
        data_dir = data_dir or settings.data_dir

    return resolve_utility_directory(
        db_value=value,
        default_parent=library_root,
        default_subdir=".trash",
        library_root=library_root,
        data_dir=data_dir,
    )


async def ensure_utility_directories(
    trash_dir: Path,
    export_dir: Path,
) -> None:
    """Create utility directories if they don't exist.

    Args:
        trash_dir: Path to the utility trash folder.
        export_dir: Path to the utility export folder.

    Raises:
        OSError: If directory creation fails (e.g., read-only parent).
    """
    for directory, name in [(trash_dir, "trash"), (export_dir, "export")]:
        directory.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "utility_directory_ensured",
            directory=str(directory),
            purpose=name,
        )


def build_trash_destination(
    trash_dir: Path,
    source: Path,
    *,
    relative_path: str | Path | None = None,
) -> Path:
    """Build the destination path for a file inside the utility trash folder."""
    if relative_path is None:
        return trash_dir / source.name

    relative = Path(relative_path)
    if relative.is_absolute():
        parts = list(relative.parts)
        relative = Path(*parts[1:]) if len(parts) > 1 else Path(source.name)

    if not relative.parts:
        relative = Path(source.name)

    return trash_dir / relative


def _dedupe_destination_path(path: Path) -> Path:
    """Return a non-conflicting filesystem path by appending a numeric suffix."""
    if not path.exists():
        return path

    parent = path.parent
    if path.suffix:
        stem = path.stem
        suffix = path.suffix
    else:
        stem = path.name
        suffix = ""

    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_path_to_utility_trash(
    source: Path,
    trash_dir: Path,
    *,
    relative_path: str | Path | None = None,
) -> Path:
    """Move a file or folder into utility trash and stamp it with the move time.

    Retention cleanup operates on the trash path's modification time, so we
    explicitly refresh the timestamp after moving to avoid immediately deleting
    older sources that were just trashed.
    """
    trash_dest = _dedupe_destination_path(
        build_trash_destination(trash_dir, source, relative_path=relative_path)
    )
    trash_dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(source), str(trash_dest))
    now = datetime.now(UTC).timestamp()
    os.utime(trash_dest, (now, now))
    return trash_dest


def move_file_to_utility_trash(
    source: Path,
    trash_dir: Path,
    *,
    relative_path: str | Path | None = None,
) -> Path:
    """Move a file into utility trash and stamp it with the move time."""
    return move_path_to_utility_trash(source, trash_dir, relative_path=relative_path)


def restore_file_from_utility_trash(
    trash_path: Path,
    restore_path: Path,
    *,
    converted_path: Path | None = None,
) -> None:
    """Restore a trashed original without deleting converted output prematurely."""
    if not trash_path.exists():
        raise FileNotFoundError(f"Original file missing from trash: {trash_path}")

    restore_path.parent.mkdir(parents=True, exist_ok=True)

    temp_backup: Path | None = None
    if converted_path is not None and converted_path.exists() and converted_path == restore_path:
        temp_backup = restore_path.with_name(f"{restore_path.name}._rollback_backup_")
        if temp_backup.exists():
            raise FileExistsError(f"Rollback staging path already exists: {temp_backup}")
        converted_path.rename(temp_backup)
    elif restore_path.exists():
        raise FileExistsError(f"Restore target already exists: {restore_path}")

    try:
        shutil.move(str(trash_path), str(restore_path))
    except Exception:
        if temp_backup is not None and temp_backup.exists() and not restore_path.exists():
            temp_backup.rename(restore_path)
        raise

    if converted_path is not None and converted_path.exists() and converted_path != restore_path:
        converted_path.unlink()

    if temp_backup is not None and temp_backup.exists():
        temp_backup.unlink()
