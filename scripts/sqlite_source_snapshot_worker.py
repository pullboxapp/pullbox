"""Standard-library-only worker executed inside the source image with read-only data."""

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path


def evidence(source):
    if source.resolve(strict=True) != source or not source.is_relative_to("/data"):
        raise ValueError
    items = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(source) + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise
            items.append(None)
            continue
        if not stat.S_ISREG(info.st_mode) or not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError
        items.append([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def run(source, capture):
    before = evidence(source)
    if not capture:
        return before
    work = Path("/pullbox-snapshot-output")
    database = source
    wal = Path(str(source) + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    rebuild = wal_size > 0 and not Path(str(source) + "-shm").exists()
    required = (source.stat().st_size + wal_size) * (2 if rebuild else 1) + 256 * 1024**2
    if shutil.disk_usage(work).free < required:
        raise OSError(errno.ENOSPC, "snapshot_space")
    if rebuild:
        # SQLite needs to reconstruct the WAL index. Never create it on the source.
        if Path(str(source) + "-journal").exists():
            raise ValueError
        database = work / "wal-copy"
        shutil.copyfile(source, database)
        shutil.copyfile(wal, work / "wal-copy-wal")
    mode = "ro" if database == source else "rw"
    if database == source and not wal.exists() and not Path(str(source) + "-journal").exists():
        # Cleanly closed WAL-mode databases may otherwise try to create an empty WAL index.
        # Never take this shortcut when SQLite needs to consult a journal.
        mode = "ro&immutable=1"
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=" + mode, uri=True)) as src,
        closing(sqlite3.connect((work / "snapshot").as_uri() + "?mode=rw", uri=True)) as dest,
    ):
        src.execute("PRAGMA trusted_schema=OFF")
        src.execute("PRAGMA query_only=ON")
        src.backup(dest, pages=256)
        if dest.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError
    if evidence(source) != before:
        raise ValueError
    return before


if __name__ == "__main__":
    try:
        print(run(Path(sys.argv[1]), sys.argv[2] == "capture"))
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            sys.exit(1)
        print("snapshot_space")
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorcode", 0) != sqlite3.SQLITE_FULL:
            sys.exit(1)
        print("snapshot_space")
    except ValueError:
        sys.exit(1)
