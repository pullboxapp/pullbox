"""Determine whether SQLite ZIP backups apply to the configured database."""

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ZIP_BACKUP_UNAVAILABLE = (
    "ZIP restore points require a file-backed SQLite database. "
    "For PostgreSQL, use pg_dump for backups and pg_restore into a new, empty database."
)


def sqlite_backup_path(db_url: str) -> Path | None:
    if db_url.startswith("/"):  # Historical standalone backup configuration.
        return Path(db_url)
    try:
        url = make_url(db_url)
    except (ArgumentError, ValueError):
        return None
    if url.get_backend_name() != "sqlite" or url.username or url.password or url.port is not None:
        return None
    name = url.database or ""
    if url.host:  # Preserve the legacy sqlite://relative/directory form.
        name = f"{url.host}/{name}" if name else url.host
    if not name or name == ":memory:" or name.startswith("file:"):
        return None
    return Path(name)
