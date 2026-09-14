"""Validate signed SQLite artifacts using the producer's fixed schema and hash.

This database is an external read-only artifact, separate from the ORM application
database. SQL identifiers below are contract constants, never user input.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pullbox.services.catalog.contract import CatalogError, valid_version

if TYPE_CHECKING:
    from pathlib import Path

TABLES = {
    "publishers": ("id,name", "id", "publisher"),
    "series": ("id,name,start_year,publisher_id,issue_count,cover_url", "id", "series"),
    "series_aliases": (
        "series_id,position,alias,normalized_alias",
        "series_id,position",
        "series_alias",
    ),
    "issues": (
        "id,series_id,issue_number,normalized_issue_number,sort_number,title,cover_date,store_date,cover_url",
        "id",
        "issue",
    ),
}
FTS_TABLES = {
    "series_fts",
    *(f"series_fts_{suffix}" for suffix in ("config", "content", "data", "docsize", "idx")),
}
REQUIRED_TABLES = {*TABLES, "series_fts", "dataset_manifest", "schema_migrations"}
# Closed SQL allowlist, generated only from contract constants. Request input
# never becomes an identifier, including in the external artifact database.
CONTENT_QUERIES = {
    table: f"SELECT {columns} FROM {table} ORDER BY {order}"
    for table, (columns, order, _) in TABLES.items()
}
COUNT_QUERIES = {table: f"SELECT COUNT(*) FROM {table}" for table in TABLES}
PATCH_DELETE_QUERIES = {
    table: f"SELECT {keys} FROM {prefix}_deletes" for table, (_, keys, prefix) in TABLES.items()
}
PATCH_UPSERT_QUERIES = {
    table: f"SELECT {columns} FROM {prefix}_upserts"
    for table, (columns, _, prefix) in TABLES.items()
}
MANIFEST_QUERIES = {
    "dataset_manifest": "SELECT key,value FROM dataset_manifest",
    "patch_manifest": "SELECT key,value FROM patch_manifest",
    "target_dataset_manifest": "SELECT key,value FROM target_dataset_manifest",
}
REBUILD_FTS = """
INSERT INTO series_fts(rowid,series_id,name,aliases)
SELECT s.id,s.id,s.name,COALESCE(GROUP_CONCAT(a.alias,' '),'')
FROM series s LEFT JOIN series_aliases a ON a.series_id=s.id
GROUP BY s.id,s.name ORDER BY s.id
"""


def safe_path(path: Path) -> Path:
    """Reject symlink components in app-owned catalog paths."""
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise CatalogError("Catalog storage contains a symbolic link. Check the data volume.")
    return path


def open_readonly(path: Path) -> sqlite3.Connection:
    safe_path(path)
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA cache_size=-8192")
    return db


def file_sha256(path: Path) -> str:
    safe_path(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(db: sqlite3.Connection, table: str = "dataset_manifest") -> dict[str, Any]:
    if table not in MANIFEST_QUERIES:
        raise CatalogError("Catalog manifest table is invalid.")
    return {str(k): json.loads(v) for k, v in db.execute(MANIFEST_QUERIES[table])}


def logical_hash(db: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in TABLES:
        for row in db.execute(CONTENT_QUERIES[table]):
            digest.update(table.encode() + b"\0")
            digest.update(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            )
    return digest.hexdigest()


def _check_database(db: sqlite3.Connection, application_id: int, allowed: set[str]) -> None:
    if (
        db.execute("PRAGMA application_id").fetchone()[0] != application_id
        or db.execute("PRAGMA user_version").fetchone()[0] != 1
    ):
        raise CatalogError("Catalog database format is not supported. Update Pullbox.")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise CatalogError("Catalog database integrity check failed. Retry the download.")
    objects = db.execute(
        "SELECT name,type FROM sqlite_master WHERE type IN ('table','view','trigger')"
    ).fetchall()
    if any(
        kind != "table" or name not in allowed
        for name, kind in objects
        if not name.startswith("sqlite_")
    ):
        raise CatalogError("Catalog contains unsupported database objects.")


def validate_snapshot(path: Path, version: str) -> dict[str, Any]:
    """Recompute logical content before allowing a new catalog to become active."""
    valid_version(version)
    try:
        with closing(open_readonly(path)) as db:
            _check_database(db, 0x50424332, REQUIRED_TABLES | FTS_TABLES)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CatalogError("Catalog has invalid record relationships.")
            manifest = read_manifest(db)
            for key, value in {
                "format_id": "pullbox-catalog-v2",
                "schema_version": "1",
                "compatibility_version": "pullbox-only",
                "application_id": 0x50424332,
                "dataset_version": version,
            }.items():
                if manifest.get(key) != value:
                    raise CatalogError("Catalog identity does not match its signed publication.")
            cutoff = datetime.fromisoformat(str(manifest.get("source_cutoff_at", "")))
            if cutoff.tzinfo is None or cutoff.strftime("%Y%m%dT%H%M%SZ") != version:
                raise CatalogError("Catalog source timestamp is invalid.")
            counts = {table: db.execute(COUNT_QUERIES[table]).fetchone()[0] for table in TABLES}
            if counts != manifest.get("counts") or logical_hash(db) != manifest.get(
                "content_sha256"
            ):
                raise CatalogError("Catalog content checksum failed. Retry the download.")
            if db.execute("SELECT COUNT(*) FROM series_fts").fetchone()[0] != counts["series"]:
                raise CatalogError("Catalog search index is incomplete.")
            if db.execute(
                "SELECT 1 FROM series s LEFT JOIN series_fts f ON f.rowid=s.id "
                "WHERE f.series_id IS NULL OR f.series_id != s.id OR f.name != s.name LIMIT 1"
            ).fetchone():
                raise CatalogError("Catalog search index is inconsistent.")
            db.execute("SELECT version,applied_at FROM schema_migrations LIMIT 1").fetchall()
            return manifest
    except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("Catalog database could not be validated. Retry the download.") from exc


def apply_catalog_patch(
    base: Path, patch: Path, output: Path, base_version: str, target_version: str
) -> dict[str, Any]:
    """Always reconstruct from the immutable weekly base, including reverted rows."""
    validate_snapshot(base, base_version)
    safe_path(output)
    try:
        with closing(open_readonly(patch)) as changes:
            allowed = {"patch_manifest", "target_dataset_manifest"}
            allowed.update(
                f"{prefix}_{kind}"
                for _, _, prefix in TABLES.values()
                for kind in ("upserts", "deletes")
            )
            _check_database(changes, 0x50425044, allowed)
            meta = read_manifest(changes, "patch_manifest")
            expected = {
                "format_id": "pullbox-catalog-v2-patch",
                "schema_version": "1",
                "base_version": base_version,
                "target_version": target_version,
                "base_snapshot_sha256": file_sha256(base),
            }
            if any(meta.get(k) != v for k, v in expected.items()):
                raise CatalogError("Catalog patch does not match the retained weekly base.")
            with base.open("rb") as source, output.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            with closing(sqlite3.connect(output)) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    db.execute("PRAGMA defer_foreign_keys=ON")
                    for table, (_, keys, _prefix) in reversed(TABLES.items()):
                        where = " AND ".join(f"{key}=?" for key in keys.split(","))
                        db.executemany(
                            f"DELETE FROM {table} WHERE {where}",
                            changes.execute(PATCH_DELETE_QUERIES[table]),
                        )
                    for table, (columns, keys, _prefix) in TABLES.items():
                        cols = columns.split(",")
                        assignments = ",".join(
                            f"{column}=excluded.{column}"
                            for column in cols
                            if column not in keys.split(",")
                        )
                        db.executemany(
                            f"INSERT INTO {table} ({columns}) "
                            f"VALUES ({','.join('?' for _ in cols)}) "
                            f"ON CONFLICT ({keys}) DO UPDATE SET {assignments}",
                            changes.execute(PATCH_UPSERT_QUERIES[table]),
                        )
                    db.execute("DELETE FROM dataset_manifest")
                    db.executemany(
                        "INSERT INTO dataset_manifest VALUES (?,?)",
                        changes.execute("SELECT key,value FROM target_dataset_manifest"),
                    )
                    db.execute("DELETE FROM series_fts")
                    db.execute(REBUILD_FTS)
            result = validate_snapshot(output, target_version)
            if result["content_sha256"] != meta.get("target_content_sha256"):
                raise CatalogError("Catalog patch target checksum failed.")
            with output.open("rb") as stream:
                os.fsync(stream.fileno())
            return result
    except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        raise CatalogError("Catalog patch could not be applied. Retry the update.") from exc
