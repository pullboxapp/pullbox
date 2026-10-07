"""Read-only GCD dump boundary; no migrations, copies or application DB attachment."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import column, table
from sqlalchemy.dialects.sqlite import dialect

from pullbox.schemas.metadata_sources import SourceStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.sql import Select
    from sqlalchemy.sql.selectable import TableClause


class GcdDatabaseError(ValueError):
    """Safe operator-facing failure; never embeds a path or SQLite error text."""

    def __init__(self, message: str, status: SourceStatus = SourceStatus.INVALID_CONFIG) -> None:
        self.status = status
        super().__init__(message)


class GcdSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    profile: Literal["gcd-sqlite-2026-09"] = "gcd-sqlite-2026-09"
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: tuple[int, int, int, int, int]
    validated_at: datetime


# Minimal semantic profile, derived from the official 2026-09-29 dump and
# GCD's Series.active_base_issues(). Additive dump columns are deliberately ignored.
SERIES = table(
    "gcd_series",
    *map(
        column,
        (
            "id",
            "name",
            "sort_name",
            "year_began",
            "year_ended",
            "publisher_id",
            "language_id",
            "is_current",
            "deleted",
            "is_comics_publication",
            "notes",
        ),
    ),
)
ISSUE = table(
    "gcd_issue",
    *map(
        column,
        (
            "id",
            "series_id",
            "number",
            "title",
            "sort_code",
            "deleted",
            "variant_of_id",
            "notes",
            "key_date",
            "on_sale_date",
            "page_count",
        ),
    ),
)
PUBLISHER = table("gcd_publisher", *map(column, ("id", "name", "deleted")))
LANGUAGE = table("stddata_language", *map(column, ("id", "code")))
PROFILE_TABLES = (SERIES, ISSUE, PUBLISHER, LANGUAGE)
_READ_SLOTS = threading.BoundedSemaphore(2)
_VALIDATION_SLOT = threading.BoundedSemaphore(1)


def optional_profile(db: sqlite3.Connection, tables: tuple[TableClause, ...]) -> bool:
    """Older minimal dumps remain readable; present enrichment tables must be real."""
    found = [
        db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (expected.name,)).fetchone()
        for expected in tables
    ]
    for expected, kind in zip(tables, found, strict=True):
        if kind is None:
            continue
        if kind[0] != "table" or not kind[1] or "VIRTUAL TABLE" in kind[1].upper():
            raise GcdDatabaseError("The optional GCD schema is incompatible.")
        actual = {
            row[0] for row in db.execute("SELECT name FROM pragma_table_info(?)", (expected.name,))
        }
        if not set(expected.c.keys()) <= actual:
            raise GcdDatabaseError("The optional GCD schema is incompatible.")
    return all(kind is not None for kind in found)


def signature(path: Path) -> tuple[int, int, int, int, int]:
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise GcdDatabaseError("Enter an absolute path to a GCD SQLite dump.")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise GcdDatabaseError("Use the real GCD database path, not a symbolic link.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size < 100:
        raise GcdDatabaseError("Choose a complete, regular GCD SQLite database file.")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise GcdDatabaseError(
            "Use a closed SQLite dump, not a database being written by another application."
        )
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def assert_current(snapshot: GcdSnapshot) -> None:
    if signature(Path(snapshot.path)) != snapshot.signature:
        raise GcdDatabaseError(
            "The GCD database changed. Validate and enable it again in Metadata settings."
        )


@contextmanager
def open_readonly(
    path: Path, stop: threading.Event, deadline: float
) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
        db.row_factory = sqlite3.Row
        yield db
    finally:
        db.close()


def rows(db: sqlite3.Connection, query: Select[Any]) -> list[sqlite3.Row]:
    # SQLAlchemy Core owns the query and binds; sqlite3 is confined to the
    # off-loop, read-only external-file boundary rather than the app database.
    compiled = query.compile(
        dialect=dialect(paramstyle="named"), compile_kwargs={"render_postcompile": True}
    )
    return db.execute(str(compiled), compiled.params).fetchall()


async def disk_read[T](
    operation: Callable[[threading.Event, float], T], *, validation: bool = False
) -> T:
    stop = threading.Event()
    slots = _VALIDATION_SLOT if validation else _READ_SLOTS
    if not slots.acquire(blocking=False):
        raise GcdDatabaseError(
            "GCD is busy. Wait for the current operation, then retry.", SourceStatus.UNAVAILABLE
        )
    deadline = time.monotonic() + (300 if validation else 5)

    def run() -> T:
        try:
            return operation(stop, deadline)
        finally:
            slots.release()

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with suppress(sqlite3.Error, GcdDatabaseError, OSError):
            await task
        raise
    except (sqlite3.Error, OSError) as exc:
        if stop.is_set() or time.monotonic() >= deadline:
            raise GcdDatabaseError(
                "GCD validation or reading timed out. Check the storage and retry.",
                SourceStatus.TIMEOUT,
            ) from exc
        raise GcdDatabaseError(
            "The GCD database cannot be read. Check the mount, permissions and dump, then retry.",
            SourceStatus.UNAVAILABLE,
        ) from exc


async def validate_candidate(value: str | None) -> GcdSnapshot:
    if not value:
        raise GcdDatabaseError("Enter the GCD SQLite database path before enabling it.")
    path = Path(value)

    def validate(stop: threading.Event, deadline: float) -> GcdSnapshot:
        initial = signature(path)
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise GcdDatabaseError(
                    "This is not a SQLite database. Obtain the official GCD SQLite dump."
                )
        with open_readonly(path, stop, deadline) as db:
            for expected in PROFILE_TABLES:
                kind = db.execute(
                    "SELECT type,sql FROM sqlite_schema WHERE name=?", (expected.name,)
                ).fetchone()
                if kind is None or kind[0] != "table" or "VIRTUAL TABLE" in kind[1].upper():
                    raise GcdDatabaseError(
                        "This SQLite dump does not have the supported GCD schema."
                    )
                actual = {
                    row[0]
                    for row in db.execute("SELECT name FROM pragma_table_info(?)", (expected.name,))
                }
                if not set(expected.c.keys()) <= actual:
                    raise GcdDatabaseError(
                        "This GCD dump is missing required columns. "
                        "Obtain a current official SQLite dump."
                    )
            if [row[0] for row in db.execute("PRAGMA quick_check(1)")] != ["ok"]:
                raise GcdDatabaseError(
                    "The GCD database failed its integrity check. Replace the dump and retry."
                )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                if stop.is_set() or time.monotonic() >= deadline:
                    raise GcdDatabaseError(
                        "GCD validation was stopped. The previous database is unchanged."
                    )
                digest.update(chunk)
        if signature(path) != initial:
            raise GcdDatabaseError(
                "The GCD database changed during validation. "
                "Wait for the file transfer to finish, then retry."
            )
        return GcdSnapshot(
            path=str(path),
            sha256=digest.hexdigest(),
            signature=initial,
            validated_at=datetime.now(UTC),
        )

    return await disk_read(validate, validation=True)
