"""Publish completed staging files without replacing an existing destination."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

_UNSUPPORTED_RENAME = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4


def publish_file_without_overwrite(stage: Path, target: Path) -> None:
    """Consume a stage using an atomic destination claim, never plain POSIX rename.

    Native exclusive rename avoids requiring hard-link support on managed-copy
    destinations. Older filesystems retain the link/unlink fallback. Neither
    path copies bytes into a visible, partially written final file.
    """
    mode = stage.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise OSError(errno.EINVAL, "Publication requires a regular file or symlink", str(stage))
    if _native_rename_without_overwrite(stage, target):
        return
    if stat.S_ISLNK(mode):
        os.symlink(os.readlink(stage), target)
    else:
        os.link(stage, target, follow_symlinks=False)
    stage.unlink()


def _native_rename_without_overwrite(stage: Path, target: Path) -> bool:
    if sys.platform == "win32":
        # Windows rename fails if the destination exists; POSIX rename does not.
        os.rename(stage, target)
        return True
    if sys.platform not in {"linux", "darwin"}:
        return False

    source_bytes, target_bytes = os.fsencode(stage), os.fsencode(target)
    if b"\0" in source_bytes or b"\0" in target_bytes:
        raise ValueError("embedded null byte")
    libc = ctypes.CDLL(None, use_errno=True)
    symbol = "renameat2" if sys.platform == "linux" else "renamex_np"
    rename = getattr(libc, symbol, None)
    if rename is None:
        return False
    rename.restype = ctypes.c_int
    if sys.platform == "linux":
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(_AT_FDCWD, source_bytes, _AT_FDCWD, target_bytes, _RENAME_NOREPLACE)
    else:
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(source_bytes, target_bytes, _RENAME_EXCL)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in _UNSUPPORTED_RENAME:
        return False
    raise OSError(error_number, os.strerror(error_number), str(stage), None, str(target))


def probe_file_publication(root: Path) -> None:
    """Exercise publication and collision protection using only disposable files."""
    with tempfile.TemporaryDirectory(prefix=".pullbox-publication-probe-", dir=root) as directory:
        stage = Path(directory) / "stage"
        target = Path(directory) / "target"
        stage.write_bytes(b"original")
        publish_file_without_overwrite(stage, target)
        stage.write_bytes(b"replacement")
        try:
            publish_file_without_overwrite(stage, target)
        except FileExistsError:
            if target.read_bytes() == b"original" and stage.read_bytes() == b"replacement":
                return
        raise OSError(errno.ENOTSUP, "Storage did not preserve an existing destination", str(root))


def publication_failure_message(directory: Path, error: OSError) -> str:
    """Actionable destination guidance shared by preflight and execution failures."""
    return (
        f"Cannot safely publish imported files in {directory}: {error.strerror or str(error)}. "
        "Check the destination mount, free space, and the container user's file permissions. "
        "The destination must support exclusive rename or hard links without replacing "
        "existing files. Correct the storage settings or choose another managed library root, "
        "then retry."
    )
