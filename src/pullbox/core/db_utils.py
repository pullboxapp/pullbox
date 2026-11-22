"""Database utility helpers — SQL wildcard escaping and query safety."""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def database_backend_label(db_url: str) -> str:
    """Return a display label, never connection credentials or URL parameters."""
    try:
        backend = make_url(db_url).get_backend_name()
    except (ArgumentError, ValueError):
        return "Unknown"
    return {"sqlite": "SQLite", "postgresql": "PostgreSQL"}.get(backend, "Unknown")


def escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters in user input.

    Prevents users from injecting ``%`` and ``_`` wildcards to manipulate
    query results or cause performance issues.  Backslashes are escaped
    first to avoid double-escaping.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
