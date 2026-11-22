"""Open a standalone SQLite backup or a ZIP produced by Pullbox Backup."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tempfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from pullbox.services.backup_service import DB_FILENAME, METADATA_FILENAME

if TYPE_CHECKING:
    from collections.abc import Iterator

_MIB = 1024 * 1024


class BackupInputError(Exception):
    """Only fixed operator-facing codes cross the report boundary."""


class _ArchiveReader(io.BufferedReader):
    reading_directory = True

    def read(self, size: int | None = -1, /) -> bytes:
        # ZipFile reads the whole central directory before it exposes member names.
        if self.reading_directory:
            requested = (
                os.fstat(self.fileno()).st_size - self.tell() if size is None or size < 0 else size
            )
            if requested > 128 * 1024:
                raise BackupInputError("invalid_backup_archive")
        return super().read(size)


def _signature(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BackupInputError("not_a_backup")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _standalone(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise BackupInputError("snapshot_sidecars")


@contextmanager
def database_snapshot(path: Path, configured_url: str) -> Iterator[tuple[Path, str | None]]:
    path = path.absolute()
    try:
        before = _signature(path)
        try:
            url = make_url(configured_url)
        except ArgumentError as exc:
            raise BackupInputError("database_config") from exc
        if url.get_backend_name() == "sqlite" and url.database:
            database = url.database
            if database.startswith("file:"):
                database = unquote(urlsplit(database).path)
            try:
                if path.samefile(database):
                    raise BackupInputError("configured_database")
            except FileNotFoundError:
                pass
        with path.open("rb") as source:
            header = source.read(16)
    except OSError as exc:
        raise BackupInputError("not_a_backup") from exc
    if header == b"SQLite format 3\0":
        _standalone(path)
        yield path, None
        _standalone(path)
    elif header.startswith(b"PK\x03\x04"):
        with tempfile.TemporaryDirectory(prefix="pullbox-preflight-") as scratch:
            dest = Path(scratch) / DB_FILENAME
            version = _unpack(path, dest)
            yield dest, version
    else:
        raise BackupInputError("invalid_database")
    if _signature(path) != before:
        raise BackupInputError("backup_changed")


def _unpack(path: Path, dest: Path) -> str:
    try:
        with _ArchiveReader(io.FileIO(path, "r")) as reader, ZipFile(reader) as archive:
            reader.reading_directory = False
            if sorted(archive.namelist()) != sorted([DB_FILENAME, METADATA_FILENAME]):
                raise BackupInputError("invalid_backup_archive")
            db, meta = archive.getinfo(DB_FILENAME), archive.getinfo(METADATA_FILENAME)
            for entry in (db, meta):
                if (
                    stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                    or entry.flag_bits & 1
                    or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                ):
                    raise BackupInputError("invalid_backup_archive")
            if not 0 < meta.file_size <= 64 * 1024 or db.file_size < 512:
                raise BackupInputError("invalid_backup_archive")
            manifest = json.loads(archive.read(meta))
            if not isinstance(manifest, dict):
                raise BackupInputError("invalid_backup_archive")
            version = manifest.get("pullbox_version")
            if (
                not isinstance(version, str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]{1,32})?", version)
                or type(manifest.get("db_size_bytes")) is not int
                or manifest["db_size_bytes"] != db.file_size
            ):
                raise BackupInputError("invalid_backup_archive")
            if (
                db.file_size > 32 * 1024 * _MIB
                or shutil.disk_usage(dest.parent).free < db.file_size + 256 * _MIB
            ):
                raise BackupInputError("scratch_space")
            # Fixed local filename inside a private directory; never extract member paths.
            with archive.open(db) as source, dest.open("xb") as target:
                shutil.copyfileobj(source, target, length=_MIB)
            if dest.stat().st_size != db.file_size:
                raise BackupInputError("invalid_backup_archive")
            return version
    except (BadZipFile, UnicodeError, ValueError, zlib.error, EOFError, RecursionError) as exc:
        raise BackupInputError("invalid_backup_archive") from exc
