"""Read a registered library file under an explicitly mapped directory, without following links."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class AssetVerificationError(Exception):
    """Fixed operator-visible reason; never include path, name, or database values."""


def hex_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{64}", value) is not None


def _relative(root: str, path: str) -> tuple[str, ...]:
    recorded, file = PurePosixPath(root), PurePosixPath(path)
    if not root.startswith("/") or not file.is_relative_to(recorded) or ".." in file.parts:
        raise AssetVerificationError("outside_library_root")
    parts = file.relative_to(recorded).parts
    if not parts or ".." in recorded.parts or "\0" in path:
        raise AssetVerificationError("unsafe_path")
    return parts


@contextmanager
def _open_regular(root_fd: int, parts: tuple[str, ...]) -> Iterator[int]:
    with ExitStack() as stack:
        directory = root_fd
        for part in parts[:-1]:
            directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, directory)
        initial = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(initial.st_mode):
            raise AssetVerificationError("unsafe_path")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stack.callback(os.close, fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(initial, opened):
            raise AssetVerificationError("file_changed")
        yield fd


def _stamp(s: os.stat_result) -> tuple[int, ...]:
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _read_hash(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise AssetVerificationError("file_changed")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise AssetVerificationError("file_changed")
    return digest.hexdigest()


def inspect_file(
    root_fd: int,
    recorded_root: str,
    path: str,
    size: int,
    saved_hash: str | None,
    signature: str,
    *,
    sample: bool,
) -> tuple[str | None, bool]:
    """Return an observed content digest and whether it matched independent historical evidence."""
    parts = _relative(recorded_root, path)
    evidence = json.loads(signature)
    if not isinstance(evidence, dict):
        raise AssetVerificationError("invalid_file_evidence")
    placement = evidence.get("content_digest")
    algorithm = evidence.get("content_digest_algorithm")
    if (placement is not None or algorithm is not None) and (
        algorithm != "sha256" or not hex_digest(placement)
    ):
        raise AssetVerificationError("invalid_file_evidence")
    if saved_hash is not None and not hex_digest(saved_hash):
        raise AssetVerificationError("invalid_file_evidence")
    hashes = {h.lower() for h in (saved_hash, placement) if h is not None}
    if len(hashes) > 1:
        raise AssetVerificationError("conflicting_file_hashes")
    try:
        with _open_regular(root_fd, parts) as fd:
            before = os.fstat(fd)
            if before.st_size != size or evidence.get("size", size) != size:
                raise AssetVerificationError("file_size_mismatch")
            if evidence.get("mtime_ns", before.st_mtime_ns) != before.st_mtime_ns:
                raise AssetVerificationError("file_timestamp_mismatch")
            observed = _read_hash(fd, size) if sample else None
            if observed is not None and hashes and observed not in hashes:
                raise AssetVerificationError("file_hash_mismatch")
            # Reopen the directory chain under the pinned root to detect replaced paths.
            with _open_regular(root_fd, parts) as current:
                if _stamp(before) != _stamp(os.fstat(current)):
                    raise AssetVerificationError("file_changed")
            return observed, bool(observed and hashes)
    except FileNotFoundError:
        raise AssetVerificationError("file_missing") from None
    except OSError as exc:
        if exc.errno in {errno.ENOTDIR, errno.ELOOP}:
            raise AssetVerificationError("unsafe_path") from None
        raise AssetVerificationError("file_unreadable") from None
