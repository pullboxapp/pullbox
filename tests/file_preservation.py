"""Snapshots for assertions that a source file was not mutated."""

from pathlib import Path


def file_preservation_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    content = path.read_bytes()
    info = path.stat()
    # Reading may update atime. Mutation evidence must still match, including
    # nanoseconds omitted by os.stat_result's whole-second tuple equality.
    return content, (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
