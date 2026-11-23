"""Offline source checks only: no engine, application startup, DDL or target writes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text

from pullbox import __version__
from pullbox.models import Base
from pullbox.models.issue import IssueType
from pullbox.services.migration_backup_input import BackupInputError, database_snapshot

if TYPE_CHECKING:
    from pathlib import Path

SOURCE_REVISION = "p3d456789abc"
# Fingerprints from empty, Alembic-migrated SQLite databases. Older snapshots
# can transfer without a rewrite if their actual classifications are canonical.
_PREVIOUS_LAYOUT = "e7cba84ac733597272281907576d8c689dc1db888b33f15e9c91f0112ebe0d66"
_SQLITE_LAYOUTS = {
    SOURCE_REVISION: "d0349255ba2d818481963d88ff371487866cd8eda447d4ab4fd776eb45f91721",
    "o2c3456789ab": _PREVIOUS_LAYOUT,
    "n1b23456789a": _PREVIOUS_LAYOUT,
}
_NEXT = (
    "Source inspection only. Live writer shutdown, storage, secrets, PostgreSQL "
    "target and transfer verification are separate checks. Do not switch databases."
)
_BLOCKERS = {
    "not_a_backup": "Select a readable regular backup file, not a directory or symlink.",
    "database_config": "Cannot identify the configured database. Check the database URL.",
    "configured_database": "This is the configured database. Select a separate verified backup.",
    "invalid_backup_archive": "Use a complete Pullbox backup ZIP with its original manifest.",
    "scratch_space": (
        "Provide temporary space for the database plus 256 MiB, or inspect an extracted "
        "standalone database (required above 32 GiB)."
    ),
    "snapshot_sidecars": "Make a fresh consistent backup. Do not delete SQLite journals.",
    "invalid_database": "The file did not pass SQLite validation. Make a fresh backup.",
    "backup_changed": "The backup changed during inspection. Retry with a stable copy.",
    "unsupported_revision": (
        "Use a backup matching this Pullbox schema. Upgrade a separate restored copy first, "
        "never the original."
    ),
    "schema_mismatch": "The source layout is not recognized. Do not transfer this backup.",
    "foreign_key_violation": "Broken database relationships. Investigate on a separate copy.",
    "inspection_io": "Check input permissions and temporary disk space, then retry.",
}

# These are durable owners and reservations, not transient UI badges. A quiet
# snapshot is useful evidence, but cannot prove that live writers have stopped.
_WORK = {
    "utility_jobs": (
        text("upper(state) NOT IN ('COMPLETED','FAILED','CANCELLED','ROLLED_BACK')"),
        "Finish or cancel outstanding Utilities jobs.",
    ),
    "import_jobs": (
        text(
            "upper(status) NOT IN ('COMPLETED','FAILED','CANCELLED','ROLLED_BACK') "
            "OR story_arc_rollback_waiting_work_id IS NOT NULL"
        ),
        "Finish or cancel outstanding imports and rollbacks.",
    ),
    "import_job_actions": (
        text("upper(status) = 'ROLLBACK_FAILED'"),
        "Resolve the failed import rollback in Import History.",
    ),
    "download_history": (
        text(
            "upper(state) NOT IN ('COMPLETED','FAILED','IMPORTED') "
            "OR (upper(state)='COMPLETED' AND imported_at IS NULL) "
            "OR post_processing_claim_token IS NOT NULL OR post_processing_claimed_at IS NOT NULL"
        ),
        "Settle active downloads and post-processing in Downloads or Recovery.",
    ),
    "direct_acquisition_attempts": (
        text(
            "upper(state) NOT IN ('DISCOVERED','COMPLETED','FAILED','CANCELLED','INTERVENTION') "
            "OR (upper(state)='DISCOVERED' AND (started_at IS NOT NULL "
            "OR next_retry_at IS NOT NULL OR plan_revision <> 0))"
        ),
        "Settle outstanding direct downloads before cutover.",
    ),
    "direct_artifact_attempts": (
        text("upper(state) NOT IN ('COMPLETED','FAILED','CANCELLED','INTERVENTION')"),
        "Settle outstanding direct download artifacts.",
    ),
    "archive_metadata_publications": (
        text(
            "active_path_key IS NOT NULL OR upper(state) NOT IN ('ABANDONED','FINALIZED','SETTLED')"
        ),
        "Resolve pending archive metadata publication or recovery.",
    ),
    "library_conversions": (
        text("active OR upper(state) NOT IN ('COMPLETE','ABANDONED')"),
        "Finish or recover pending library conversions.",
    ),
    "library_removals": (
        text("active OR upper(state) NOT IN ('COMPLETE','ABANDONED')"),
        "Finish or recover pending library file removals.",
    ),
    "story_arc_sync_work": (
        text("upper(state) NOT IN ('COMPLETED','FAILED','CANCELLED') OR claim_token IS NOT NULL"),
        "Settle outstanding Story Arc file synchronization.",
    ),
    "story_arc_placements": (
        text(
            "operation_token IS NOT NULL "
            "OR json_extract(last_result, '$.trash_receipt') IS NOT NULL"
        ),
        "Finish interrupted Story Arc file operations.",
    ),
    "story_arcs": (
        text("json_type(diagnostics,'$.pending_member_cleanup')='object'"),
        "Finish interrupted Story Arc comic cleanup.",
    ),
}


@dataclass
class PreflightReport:
    inspector_version: str = __version__
    backup_version: str | None = None
    source_revision: str | None = None
    supported_revision: str = SOURCE_REVISION
    database_bytes: int | None = None
    integrity_ok: bool | None = None
    foreign_keys_ok: bool | None = None
    tables: dict[str, int] = field(default_factory=dict)
    blockers: dict[str, str] = field(default_factory=dict)
    source_checks_passed: bool = False
    cutover_ready: bool = False
    next_step: str = _NEXT


def _layout_digest(db: sqlite3.Connection) -> str:
    tables = set(Base.metadata.tables) | {"alembic_version"}
    public = {
        name
        for schema, name, kind, *_ in db.execute("PRAGMA table_list")
        if schema == "main" and not name.startswith("sqlite_") and kind == "table"
    }
    if (
        public != tables
        or db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type NOT IN ('table','index') LIMIT 1"
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM pragma_table_list WHERE schema='main' "
            "AND type IN ('virtual','shadow') LIMIT 1"
        ).fetchone()
    ):
        raise BackupInputError("schema_mismatch")
    shape: list[object] = []
    for name in sorted(tables):
        columns = db.execute(
            'SELECT name,upper(type),"notnull",dflt_value,pk,hidden '
            "FROM pragma_table_xinfo(?) ORDER BY cid",
            (name,),
        ).fetchall()
        groups: dict[int, list[tuple[object, ...]]] = {}
        for fid, seq, *edge in db.execute(
            "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id,seq", (name,)
        ):
            groups.setdefault(fid, []).append((seq, *edge))
        indexes = []
        for idx, unique, origin, partial in db.execute(
            'SELECT name,"unique",origin,partial FROM pragma_index_list(?)', (name,)
        ):
            keys = db.execute(
                "SELECT cid,name,desc,coll FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno",
                (idx,),
            ).fetchall()
            ddl = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (idx,)
            ).fetchone()
            indexes.append(json.dumps([unique, origin, partial, keys, ddl[0] if ddl else None]))
        shape.append([name, columns, sorted(groups.values(), key=str), sorted(indexes)])
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()


def inspect_snapshot_connection(db: sqlite3.Connection, report: PreflightReport) -> None:
    """Inspect the same immutable backup connection that an offline rehearsal will read."""
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    if not db.execute(
        "SELECT 1 FROM pragma_table_list WHERE schema='main' "
        "AND name='alembic_version' AND type='table'"
    ).fetchone():
        raise BackupInputError("unsupported_revision")
    versions = db.execute("SELECT version_num FROM alembic_version LIMIT 2").fetchall()
    if (
        len(versions) == 1
        and isinstance(versions[0][0], str)
        and re.fullmatch(r"[a-z0-9]{12}", versions[0][0])
    ):
        report.source_revision = versions[0][0]
    if report.source_revision is None or report.source_revision not in _SQLITE_LAYOUTS:
        raise BackupInputError("unsupported_revision")
    if _layout_digest(db) != _SQLITE_LAYOUTS[report.source_revision]:
        raise BackupInputError("schema_mismatch")
    names = tuple(kind.name for kind in IssueType)
    if db.execute(
        "SELECT 1 FROM issues WHERE issue_type NOT IN ("
        + ",".join("?" for _ in names)
        + ") LIMIT 1",
        names,
    ).fetchone():
        report.blockers["issue_classification"] = (
            "An issue classification is not readable by Pullbox. Upgrade a separately restored "
            "copy to normalize known classifications, review any unknown values, then check again."
        )
    report.integrity_ok = db.execute("PRAGMA integrity_check(1)").fetchone() == ("ok",)
    if not report.integrity_ok:
        raise BackupInputError("invalid_database")
    report.foreign_keys_ok = db.execute("PRAGMA foreign_key_check").fetchone() is None
    if not report.foreign_keys_ok:
        report.blockers["foreign_key_violation"] = _BLOCKERS["foreign_key_violation"]
    if db.execute(
        "SELECT 1 FROM import_jobs WHERE NOT json_valid(source_layout_snapshot) LIMIT 1"
    ).fetchone():
        report.blockers["invalid_import_layout"] = (
            "An import job has malformed source layout JSON. Keep the backup intact "
            "and review the import history on a separately restored copy before transfer."
        )
    # Only application-owned identifiers and fixed predicates, never input names or values.
    inventory = {
        name: select(func.count()).select_from(table)
        for name, table in sorted(Base.metadata.tables.items())
    }
    for name, query in inventory.items():
        report.tables[name] = db.execute(str(query)).fetchone()[0]
    for name, (predicate, action) in _WORK.items():
        count = db.execute(str(inventory[name].where(predicate))).fetchone()[0]
        if count:
            if name == "import_jobs":
                reviews = db.execute(
                    "SELECT count(*) FROM import_jobs WHERE upper(status)='REVIEW' "
                    "AND story_arc_rollback_waiting_work_id IS NULL"
                ).fetchone()[0]
                if reviews:
                    guidance = (
                        f"{reviews} awaiting Step 3. In the original SQLite instance, go to "
                        "Import > History and choose Open review. Resolve decisions or import "
                        "the ready files; deferred files stay in Follow-up after completion."
                    )
                    action = f"{guidance} {action}" if reviews < count else guidance
            report.blockers[name] = f"{count} outstanding. {action} Then take a fresh backup."


def inspect_migration_backup(path: Path, *, configured_url: str) -> PreflightReport:
    report = PreflightReport()
    try:
        with database_snapshot(path, configured_url) as (snapshot, report.backup_version):
            report.database_bytes = snapshot.stat().st_size
            # immutable forbids SQLite from creating auxiliary files even for a backup
            # carrying a WAL-mode header. The boundary rejects adjacent journals.
            uri = snapshot.as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True)) as db:
                inspect_snapshot_connection(db, report)
    except BackupInputError as exc:
        code = str(exc)
        report.blockers[code] = _BLOCKERS[code]
    except sqlite3.Error:
        report.blockers["invalid_database"] = _BLOCKERS["invalid_database"]
    except OSError:
        report.blockers["inspection_io"] = _BLOCKERS["inspection_io"]
    report.source_checks_passed = not report.blockers
    return report
