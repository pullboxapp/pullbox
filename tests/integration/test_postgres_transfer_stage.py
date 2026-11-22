"""Persistent staging never opens a target for normal application use."""

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_transfer_rehearsal as rehearsals

server = targets.server
backup = rehearsals.backup
ROOT = rehearsals.ROOT


def stage(source, url):
    with source.open("rb") as f:
        before = hashlib.file_digest(f, "sha256").digest()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/stage_postgres_transfer.py",
            "--backup",
            str(source),
            "--create-offline-stage",
            "--url-stdin",
        ],
        cwd=ROOT,
        input=url + "\n",
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PULLBOX_DB_URL": "sqlite:////not-the-running-db"},
    )
    assert not result.stderr
    assert url not in result.stdout and "Private title" not in result.stdout
    with source.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").digest() == before
    return result.returncode, json.loads(result.stdout)


def target(server):
    _, password, port, sql = server
    db = "stage_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    return db, f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{db}", sql


def state(sql, db):
    return sql(
        "postgres",
        "SELECT datallowconn::text || ':' || coalesce("
        f"shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='{db}'",
    )


def bootstrap_is_blocked(url):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pullbox.docker_entrypoint import _run_migrations; _run_migrations()",
        ],
        cwd=ROOT,
        env={**os.environ, "PULLBOX_DB_URL": url},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "not currently accepting connections" in result.stdout + result.stderr


def test_verified_stage_persists_but_is_not_a_running_library(server, backup):
    db, url, sql = target(server)
    code, report = stage(backup, url)
    assert code == 0, report
    assert report["rows_verified"] and not report["cutover_ready"]
    assert state(sql, db) == "false:pullbox:offline-stage:rows-verified"
    assert report["verified_tables"]["issues"] == 1
    # Use a disposable clone for inspection. Do not unlock the staged target.
    clone = "check_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {clone} TEMPLATE {db}")
    assert sql(clone, "SELECT title FROM series WHERE id=71") == "Private title"
    with backup.open("rb") as f:
        expected_hash = hashlib.file_digest(f, "sha256").hexdigest()
    assert report["snapshot_sha256"] == expected_hash
    assert sql(clone, "SELECT snapshot_sha256 FROM pullbox_stage.verification") == expected_hash
    assert sql(clone, "SELECT source_layout_snapshot::text FROM import_jobs WHERE id=3") == (
        '{"mode":"auto","preset":null}'
    )
    assert (
        sql(clone, "SELECT future_root_policy_snapshot::text FROM import_jobs WHERE id=3") == "null"
    )
    assert sql(clone, "SELECT nextval(pg_get_serial_sequence('story_arcs','id'))") == "877"
    assert stage(backup, url)[0] == 1
    bootstrap_is_blocked(url)


def test_unrepresentable_row_leaves_persistent_stage_closed(server, backup):
    db, url, sql = target(server)
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute("UPDATE series SET monitored=2 WHERE id=71")
    code, report = stage(backup, url)
    assert code == 1 and "unrepresentable_value:series.monitored" in report["blockers"]
    assert state(sql, db) == "false:pullbox:offline-stage:incomplete"
    assert not report["rows_verified"] and not report["cutover_ready"]
    bootstrap_is_blocked(url)


def test_existing_tables_are_never_reserved_or_truncated(server, backup):
    db, url, sql = target(server)
    sql(db, "CREATE TABLE existing_library AS SELECT 73 AS evidence")
    code, report = stage(backup, url)
    assert code == 1 and "target_not_empty" in report["blockers"]
    assert state(sql, db) == "true:"
    assert sql(db, "SELECT evidence FROM existing_library") == "73"


def test_unsettled_file_journal_blocks_before_target_connection(backup):
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute(
            "INSERT INTO archive_metadata_publications "
            "(operation_id,active_path_key,revision,state,plan_json) "
            "VALUES('fixture','private file',1,'review','{}')"
        )
    code, report = stage(backup, "postgresql+asyncpg://owner@127.0.0.1:1/unreachable")
    assert code == 1 and "archive_metadata_publications" in report["blockers"]
    assert not report["target_closed"] and not report["rows_verified"]


def test_killed_copy_does_not_activate_partial_library(server, backup):
    db, url, sql = target(server)
    with backup.open("rb") as f:
        before = hashlib.file_digest(f, "sha256").digest()
    # Interrupt the real transaction after all rows were inserted, before commit.
    code = """
import asyncio, os, signal, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
import stage_postgres_transfer as stage
import postgres_rehearsal_rows as rows
async def kill(*args):
    os.kill(os.getpid(), signal.SIGKILL)
rows._verify_table = kill
asyncio.run(stage.stage(Path(sys.argv[1]), sys.stdin.readline().strip(), {}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(backup)],
        input=url + "\n",
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == -signal.SIGKILL
    assert state(sql, db) == "false:pullbox:offline-stage:incomplete"
    bootstrap_is_blocked(url)
    clone = "check_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {clone} TEMPLATE {db}")
    assert sql(clone, "SELECT count(*) FROM series") == "0"
    with backup.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").digest() == before


def test_existing_comment_is_preserved(server, backup):
    db, url, sql = target(server)
    sql("postgres", f"COMMENT ON DATABASE {db} IS 'Reserved for someone else'")
    code, report = stage(backup, url)
    assert code == 1 and "target_already_labelled" in report["blockers"]
    assert state(sql, db) == "true:Reserved for someone else"


async def test_target_claimed_during_reservation_is_left_open(server, backup, monkeypatch):
    db, url, sql = target(server)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import stage_postgres_transfer as staging

    connect = staging._connect

    async def claim_before_control(resources, locator, name):
        if name == "pullbox_stage_control":
            sql(db, "CREATE TABLE another_library AS SELECT 83 AS evidence")
        return await connect(resources, locator, name)

    monkeypatch.setattr(staging, "_connect", claim_before_control)
    report = {}
    await staging.stage(backup, url, report)
    assert "target_not_empty" in report["blockers"]
    assert state(sql, db) == "true:"
    assert sql(db, "SELECT evidence FROM another_library") == "83"


async def test_backup_changed_after_copy_is_not_marked_verified(server, backup, monkeypatch):
    db, url, sql = target(server)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import stage_postgres_transfer as staging

    open_backup = staging.database_snapshot

    @contextmanager
    def changed_backup(*args):
        with open_backup(*args) as snapshot:
            yield snapshot
            os.utime(backup, None)

    monkeypatch.setattr(staging, "database_snapshot", changed_backup)
    with pytest.raises(staging.BackupInputError, match="backup_changed"):
        await staging.stage(backup, url, {})
    assert state(sql, db) == "false:pullbox:offline-stage:incomplete"
    bootstrap_is_blocked(url)


@pytest.mark.asyncio
async def test_active_session_is_left_alone(server, backup):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    db, url, sql = target(server)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            code, report = stage(backup, url)
            assert code == 1 and "target_busy" in report["blockers"]
            assert await conn.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()
    assert state(sql, db) == "true:"


async def test_retained_copy_requires_closure_even_when_called_directly(server, backup):
    import asyncpg

    from pullbox.services.database_migration_preflight import (
        PreflightReport,
        inspect_snapshot_connection,
    )
    from scripts.postgres_rehearsal_rows import RehearsalError, compare_rows

    db, url, sql = target(server)
    targets._migrate(url, "upgrade", "head")
    sql(
        db,
        "INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
        "VALUES(19,'Existing library','existing','ENDED',0,false)",
    )
    pg = await asyncpg.connect(url.replace("postgresql+asyncpg:", "postgresql:", 1))
    try:
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
            checks = PreflightReport()
            inspect_snapshot_connection(source, checks)
            with pytest.raises(RehearsalError, match="target_not_closed"):
                await compare_rows(source, pg, checks.tables, retain=True)
    finally:
        await pg.close()
    assert sql(db, "SELECT title FROM series WHERE id=19") == "Existing library"
