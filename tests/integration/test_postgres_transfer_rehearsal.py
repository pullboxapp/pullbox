"""Exercise the operator's transfer rehearsal without passing it a target database."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def backup(tmp_path):
    db = tmp_path / "source.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "PULLBOX_DB_URL": f"sqlite+aiosqlite:///{db}"},
        capture_output=True,
        check=True,
    )
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executemany(
            "INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
            "VALUES (?,?,?,'ENDED',1,true)",
            [(71, "Private title", "a"), (99, "Parent", "b")],
        )
        conn.execute("UPDATE series SET parent_series_id=99 WHERE id=71")
        conn.execute("UPDATE series SET series_type='VOLUME' WHERE id=99")
        conn.execute(
            "INSERT INTO issues(id,series_id,issue_number,status,release_date) "
            "VALUES(83,71,0.5,'OWNED','1995-06-01')"
        )
        conn.execute(
            "UPDATE system_config SET value='private token' WHERE key='search_log_retention_days'"
        )
        conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('story_arcs',876)")
        conn.execute(
            "INSERT INTO import_jobs(id,source_path,source_type,status,scan_total_files,"
            "scan_total_dirs,"
            "series_found,series_duplicate,series_matched,series_no_match,series_new,series_imported,"
            "series_failed,search_on_add,cv_match_threshold,auto_accept_high_confidence,"
            "skip_no_match) "
            "VALUES(3,'/fixture','FILESYSTEM','COMPLETED',140,1,1,0,1,0,1,1,0,false,0.8,false,false)"
        )
        conn.execute(
            "INSERT INTO import_series(id,import_job_id,status,raw_series_name,"
            "file_count,has_files)"
            "VALUES(9,3,'IMPORTED','Fixture',140,true)"
        )
        # Normal wizard writes supply their own layout snapshot, including explicit JSON nulls.
        conn.execute(
            "UPDATE import_jobs SET source_layout_snapshot=?,future_root_policy_snapshot=? "
            "WHERE id=3",
            ('{"mode":"auto","preset":null}', "null"),
        )
        conn.executemany(
            "INSERT INTO import_files(id,import_job_id,import_series_id,file_path,file_name,"
            "file_size,"
            "file_format,has_comicinfo,status,is_preferred) VALUES(?,3,9,?,'item.cbz',?,"
            "'cbz',false,?,false)",
            [
                (
                    i,
                    f"/fixture/{i}.cbz",
                    4227910479 if i == 139 else 32,
                    ("ALREADY_OWNED", "DUPLICATE_FILE", "SAFETY_APPROVED")[i % 3],
                )
                for i in range(140)
            ],
        )
    return db


def run(db, major):
    with db.open("rb") as source:
        before = hashlib.file_digest(source, "sha256").digest()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/rehearse_postgres_transfer.py",
            "--backup",
            str(db),
            "--postgres-major",
            str(major),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PULLBOX_DB_URL": "sqlite:////not-the-snapshot.db"},
    )
    assert "Private title" not in result.stdout + result.stderr
    assert "private token" not in result.stdout + result.stderr
    assert not result.stderr
    with db.open("rb") as source:
        assert before == hashlib.file_digest(source, "sha256").digest()
    assert not list(db.parent.glob(db.name + "-*"))
    return result.returncode, json.loads(result.stdout)


@pytest.mark.parametrize("major", [16, 18])
def test_complete_verified_rehearsal(backup, major):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("opt in to disposable PostgreSQL containers")
    if major == 16:
        # An older schema with canonical issue labels still transfers without rewriting rows.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "alembic/alembic.ini",
                "downgrade",
                "n1b23456789a",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            env={**os.environ, "PULLBOX_DB_URL": f"sqlite+aiosqlite:///{backup}"},
        )
    code, report = run(backup, major)
    assert code == 0, report
    assert report["rehearsal_passed"] and not report["cutover_ready"]
    assert len(report["verified_tables"]) == 69
    assert report["verified_tables"]["series"] == 2 and report["verified_tables"]["issues"] == 1
    assert report["next_ids"]["story_arcs"] == 877
    assert report["verified_tables"]["import_files"] == 140


def test_unresolved_file_work_blocks_before_starting_target(backup):
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute(
            "INSERT INTO archive_metadata_publications "
            "(operation_id,active_path_key,revision,state,plan_json) "
            "VALUES('private operation','private path',1,'review','{}')"
        )
    code, report = run(backup, 16)
    assert code == 1 and "archive_metadata_publications" in report["blockers"]
    assert not report["target_started"] and not report["cutover_ready"]


def test_malformed_saved_layout_is_reported_without_guessing(backup):
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute(
            "UPDATE import_jobs SET source_layout_snapshot=?",
            ('{"schema_version"NULL,"mode":"auto"}',),
        )
    code, report = run(backup, 16)
    assert code == 1 and "invalid_import_layout" in report["blockers"]
    assert not report["target_started"]


def test_unrepresentable_value_blocks_without_silently_converting(backup):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("opt in to disposable PostgreSQL containers")
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute("UPDATE series SET monitored=2 WHERE id=71")
    code, report = run(backup, 16)
    assert code == 1 and "unrepresentable_value:series.monitored" in report["blockers"]
    assert report["target_started"] and not report["rehearsal_passed"]


async def test_same_count_different_content_fails_verification():
    from scripts.postgres_rehearsal_rows import (
        RehearsalError,
        TableSnapshot,
        _hash_row,
        _verify_table,
    )

    class Target:
        async def cursor(self, query, *, prefetch):
            yield (71, "changed")

    expected = hashlib.sha256()
    _hash_row(expected, (71, "original"))
    with pytest.raises(RehearsalError, match="row_mismatch:series"):
        await _verify_table(Target(), "series", TableSnapshot(1, expected.digest(), "test"))
