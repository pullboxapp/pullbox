"""Small deterministic fixtures for the published catalog v2 wire contract."""

import hashlib
import json
import sqlite3

TABLES = {
    "publishers": ("id,name", "id"),
    "series": ("id,name,start_year,publisher_id,issue_count,cover_url", "id"),
    "series_aliases": ("series_id,position,alias,normalized_alias", "series_id,position"),
    "issues": (
        "id,series_id,issue_number,normalized_issue_number,sort_number,title,cover_date,store_date,cover_url",
        "id",
    ),
}
SCHEMA = """
PRAGMA application_id=1346519858;
PRAGMA user_version=1;
CREATE TABLE publishers(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE series(id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_year INTEGER,
 publisher_id INTEGER REFERENCES publishers(id), issue_count INTEGER, cover_url TEXT);
CREATE TABLE series_aliases(series_id INTEGER REFERENCES series(id), position INTEGER,
 alias TEXT NOT NULL, normalized_alias TEXT NOT NULL, PRIMARY KEY(series_id,position));
CREATE TABLE issues(id INTEGER PRIMARY KEY, series_id INTEGER REFERENCES series(id),
 issue_number TEXT, normalized_issue_number TEXT, sort_number TEXT, title TEXT,
 cover_date TEXT, store_date TEXT, cover_url TEXT);
CREATE VIRTUAL TABLE series_fts USING fts5(series_id UNINDEXED,name,aliases,
 tokenize='porter unicode61 remove_diacritics 1');
CREATE TABLE dataset_manifest(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL);
"""


def content_hash(db):
    digest = hashlib.sha256()
    for table, (columns, order) in TABLES.items():
        for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}"):
            digest.update(table.encode() + b"\0")
            digest.update(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            )
    return digest.hexdigest()


def build_snapshot(path, version="20260913T050000Z", name="Batman", *, extra_issue=False):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO publishers VALUES (1,'DC')")
        db.execute("INSERT INTO series VALUES (10,?,2016,1,1,NULL)", (name,))
        db.execute("INSERT INTO series_aliases VALUES (10,0,'The Dark Knight','the dark knight')")
        db.execute(
            "INSERT INTO issues VALUES (100,10,'½','0.5','0.5','Rebirth','2016-08-01',NULL,NULL)"
        )
        if extra_issue:
            db.execute("INSERT INTO issues VALUES (101,10,'2','2','2','Second',NULL,NULL,NULL)")
        db.execute(
            "INSERT INTO series_fts(rowid,series_id,name,aliases) "
            "VALUES (10,10,?,'The Dark Knight')",
            (name,),
        )
        manifest = {
            "application_id": 0x50424332,
            "format_id": "pullbox-catalog-v2",
            "schema_version": "1",
            "compatibility_version": "pullbox-only",
            "dataset_version": version,
            "source_cutoff_at": f"{version[:4]}-{version[4:6]}-{version[6:8]}T05:00:00+00:00",
            "counts": {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
            },
            "content_sha256": content_hash(db),
        }
        db.executemany(
            "INSERT INTO dataset_manifest VALUES (?,?)",
            [(k, json.dumps(v)) for k, v in manifest.items()],
        )
        db.execute("INSERT INTO schema_migrations VALUES ('1','2026-09-13T05:00:00+00:00')")
    return manifest


def build_patch(path, base, target):
    names = {
        "publishers": "publisher",
        "series": "series",
        "series_aliases": "series_alias",
        "issues": "issue",
    }
    with (
        sqlite3.connect(path) as db,
        sqlite3.connect(base) as before,
        sqlite3.connect(target) as after,
    ):
        db.execute("PRAGMA application_id=1346523204")
        db.execute("PRAGMA user_version=1")
        for table, (columns, key_columns) in TABLES.items():
            before.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            # Patch upserts use the same columns but do not carry snapshot foreign keys.
            cols = columns.split(",")
            keys = key_columns.split(",")
            prefix = names[table]
            db.execute(f"CREATE TABLE {prefix}_upserts ({','.join(cols)})")
            db.execute(f"CREATE TABLE {prefix}_deletes ({key_columns})")
            old = {
                tuple(row[cols.index(k)] for k in keys): row
                for row in before.execute(f"SELECT {columns} FROM {table}")
            }
            new = {
                tuple(row[cols.index(k)] for k in keys): row
                for row in after.execute(f"SELECT {columns} FROM {table}")
            }
            for key, row in new.items():
                if old.get(key) != row:
                    db.execute(
                        f"INSERT INTO {prefix}_upserts VALUES ({','.join('?' for _ in cols)})", row
                    )
            for key in old.keys() - new.keys():
                db.execute(
                    f"INSERT INTO {prefix}_deletes VALUES ({','.join('?' for _ in keys)})", key
                )
        db.execute("CREATE TABLE target_dataset_manifest(key TEXT PRIMARY KEY,value TEXT)")
        db.executemany(
            "INSERT INTO target_dataset_manifest VALUES (?,?)",
            after.execute("SELECT * FROM dataset_manifest"),
        )
        before_meta = {
            k: json.loads(v) for k, v in before.execute("SELECT * FROM dataset_manifest")
        }
        after_meta = {k: json.loads(v) for k, v in after.execute("SELECT * FROM dataset_manifest")}
        manifest = {
            "format_id": "pullbox-catalog-v2-patch",
            "schema_version": "1",
            "base_version": before_meta["dataset_version"],
            "target_version": after_meta["dataset_version"],
            "base_snapshot_sha256": hashlib.sha256(base.read_bytes()).hexdigest(),
            "target_content_sha256": after_meta["content_sha256"],
        }
        db.execute("CREATE TABLE patch_manifest(key TEXT PRIMARY KEY,value TEXT)")
        db.executemany(
            "INSERT INTO patch_manifest VALUES (?,?)",
            [(k, json.dumps(v)) for k, v in manifest.items()],
        )
