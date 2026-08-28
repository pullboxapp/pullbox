"""Fail-closed mapping for untrusted AirDC++ completed-file targets."""

from __future__ import annotations

import stat
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from pullbox.core.file_safety import DEFAULT_ALLOWED_EXTENSIONS


class AirDcppPathMappingError(ValueError):
    """A completed target cannot be proven safe for local import."""


def map_airdcpp_completed_path(
    *,
    remote_target: str,
    remote_root: str,
    local_root: str,
    allowed_extensions: set[str] | frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS,
    require_file: bool = True,
) -> Path:
    """Map one exact remote child and prove local filesystem containment."""
    target, configured_remote = _remote_paths(remote_target, remote_root)
    if ".." in target.parts or ".." in configured_remote.parts:
        raise AirDcppPathMappingError("AirDC++ completed path contains traversal")
    try:
        relative = target.relative_to(configured_remote)
    except ValueError as exc:
        raise AirDcppPathMappingError(
            "AirDC++ completed path is outside the configured remote root"
        ) from exc
    if not relative.parts:
        raise AirDcppPathMappingError("AirDC++ completed path identifies the remote root")
    if relative.suffix.casefold() not in {suffix.casefold() for suffix in allowed_extensions}:
        raise AirDcppPathMappingError("AirDC++ completed file extension is not allowed")

    configured_local = Path(local_root).expanduser()
    if not configured_local.is_absolute() or configured_local == Path(configured_local.anchor):
        raise AirDcppPathMappingError("AirDC++ local root is unsafe")
    if configured_local.is_symlink():
        raise AirDcppPathMappingError("AirDC++ local root may not be a symlink")
    try:
        resolved_root = configured_local.resolve(strict=True)
    except OSError as exc:
        raise AirDcppPathMappingError("AirDC++ local root is unavailable") from exc
    if not resolved_root.is_dir():
        raise AirDcppPathMappingError("AirDC++ local root is not a directory")

    candidate = configured_local.joinpath(*relative.parts)
    cursor = configured_local
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise AirDcppPathMappingError("AirDC++ completed path contains a symlink")
    try:
        resolved_candidate = candidate.resolve(strict=require_file)
    except OSError as exc:
        raise AirDcppPathMappingError("AirDC++ completed file is unavailable") from exc
    if not resolved_candidate.is_relative_to(resolved_root) or resolved_candidate == resolved_root:
        raise AirDcppPathMappingError("AirDC++ completed path escapes the local root")
    if require_file:
        try:
            mode = resolved_candidate.stat().st_mode
        except OSError as exc:
            raise AirDcppPathMappingError("AirDC++ completed file is unavailable") from exc
        if not stat.S_ISREG(mode):
            raise AirDcppPathMappingError("AirDC++ completed target is not a regular file")
    return resolved_candidate


def _remote_paths(remote_target: str, remote_root: str) -> tuple[PurePath, PurePath]:
    if not remote_target or not remote_root or "\x00" in remote_target or "\x00" in remote_root:
        raise AirDcppPathMappingError("AirDC++ completed path is invalid")
    windows_root = PureWindowsPath(remote_root)
    if windows_root.is_absolute():
        target: PurePath = PureWindowsPath(remote_target)
        root: PurePath = windows_root
    else:
        target = PurePosixPath(remote_target.replace("\\", "/"))
        root = PurePosixPath(remote_root.replace("\\", "/"))
    if not target.is_absolute() or not root.is_absolute() or len(root.parts) <= 1:
        raise AirDcppPathMappingError("AirDC++ remote paths must be absolute and bounded")
    if type(target) is not type(root):
        raise AirDcppPathMappingError("AirDC++ remote path formats do not match")
    return target, root
