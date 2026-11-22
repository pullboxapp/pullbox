"""Exercise the operator command against the actual migrated SQLite schema."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from pullbox import cli
from pullbox.services.backup_service import BackupService


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    root = tmp_path_factory.mktemp("preflight-schema")
    path = root / "schema.db"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(Path(__file__).resolve().parents[2] / "alembic/alembic.ini"),
            "upgrade",
            "head",
        ],
        env={**os.environ, "PULLBOX_DB_URL": f"sqlite+aiosqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def backup(migrated, tmp_path):
    path = tmp_path / "library #1?.db"
    shutil.copyfile(migrated, path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO series(title,sort_title,status,issue_count,monitored) "
            "VALUES(?,?,'ONGOING',1,1)",
            ("private library title", "s"),
        )
    return path


def run_cli(monkeypatch, capsys, path):
    capsys.readouterr()
    monkeypatch.setenv("PULLBOX_DB_URL", "postgresql://DO-NOT-CONNECT")
    monkeypatch.setattr(
        sys, "argv", ["pullbox", "migration-preflight", "--backup", str(path), "--json"]
    )
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    else:
        code = 0
    captured = capsys.readouterr()
    assert code != 2, "Pullbox does not expose the migration preflight command"
    assert "private library title" not in captured.out and "DO-NOT-CONNECT" not in captured.out
    return code, json.loads(captured.out)


@pytest.mark.parametrize("zipped", [False, True])
def test_operator_can_inspect_current_backup_without_writes(
    backup, tmp_path, zipped, monkeypatch, capsys
):
    source = backup
    if zipped:
        result = BackupService(tmp_path / "exports", backup).create_backup()
        source = tmp_path / "exports" / result.filename
        capsys.readouterr()
    before = hashlib.sha256(source.read_bytes()).digest(), source.stat().st_mtime_ns
    code, report = run_cli(monkeypatch, capsys, source)
    assert code == 0 and report["source_checks_passed"]
    assert report["integrity_ok"] and report["foreign_keys_ok"]
    assert not report["cutover_ready"] and report["tables"]["series"] == 1
    assert "issue_external_identities" in report["tables"] and "utility_jobs" in report["tables"]
    assert report["database_bytes"] == backup.stat().st_size and not report["blockers"]
    assert before == (hashlib.sha256(source.read_bytes()).digest(), source.stat().st_mtime_ns)
    assert not list(source.parent.glob(source.name + "-*"))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("revision", "unsupported_revision"),
        ("table", "schema_mismatch"),
        ("column", "schema_mismatch"),
        ("view", "schema_mismatch"),
        ("fk", "foreign_key_violation"),
        ("job", "utility_jobs"),
        ("publication", "archive_metadata_publications"),
        ("wal", "snapshot_sidecars"),
        ("journal", "snapshot_sidecars"),
        ("invalid", "invalid_database"),
        ("missing", "not_a_backup"),
        ("duplicate_zip", "invalid_backup_archive"),
        ("wrong_size", "invalid_backup_archive"),
    ],
)
def test_blocks_unsafe_or_incomplete_backups(backup, tmp_path, change, reason, monkeypatch, capsys):
    sql = {
        "revision": "UPDATE alembic_version SET version_num='i6c7d8e9f012'",
        "table": "CREATE TABLE unreviewed(payload TEXT)",
        "column": "ALTER TABLE users ADD COLUMN unreviewed TEXT",
        "view": "CREATE VIEW unexpected AS SELECT * FROM users",
        "fk": "INSERT INTO issues(series_id,issue_number,status) VALUES(999,1,'WANTED')",
        "job": (
            "INSERT INTO utility_jobs(id,job_type,state,display_name) "
            "VALUES('j','file_convert','CANCELLING','private library title')"
        ),
        "publication": (
            "INSERT INTO archive_metadata_publications"
            "(operation_id,active_path_key,revision,state,plan_json) "
            "VALUES('p','path',1,'review','{}')"
        ),
    }
    if change in sql:
        with closing(sqlite3.connect(backup)) as db, db:
            db.execute(sql[change])
    elif change in {"wal", "journal"}:
        Path(str(backup) + "-" + change).write_bytes(b"preserve sidecar")
    elif change == "invalid":
        backup.write_bytes(b"not SQLite")
    elif change == "missing":
        backup = tmp_path / "not-created.db"
    else:
        source = tmp_path / "archive.zip"
        with ZipFile(source, "w", ZIP_DEFLATED) as archive:
            archive.write(backup, "pullbox.db")
            archive.writestr(
                "backup_info.json", json.dumps({"db_size_bytes": 1, "pullbox_version": "2.0.0-dev"})
            )
            if change == "duplicate_zip":
                with pytest.warns(UserWarning, match="Duplicate"):
                    archive.writestr("pullbox.db", b"replacement")
        backup = source
    code, report = run_cli(monkeypatch, capsys, backup)
    assert code == 1 and not report["source_checks_passed"] and not report["cutover_ready"]
    assert reason in report["blockers"]
    if change == "missing":
        assert not backup.exists()


def test_refuses_configured_database_even_through_a_hardlink(backup, tmp_path, monkeypatch, capsys):
    link = tmp_path / "not-a-backup.db"
    os.link(backup, link)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(db_url=f"sqlite:///{link}"))
    code, report = run_cli(monkeypatch, capsys, backup)
    assert code == 1 and "configured_database" in report["blockers"]


def test_backup_includes_committed_wal_without_accepting_a_live_file(
    backup, tmp_path, monkeypatch, capsys
):
    with closing(sqlite3.connect(backup)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute(
            "INSERT INTO series(title,sort_title,status,issue_count,monitored) "
            "VALUES('new in WAL','w','ONGOING',0,1)"
        )
        writer.commit()
        assert Path(str(backup) + "-wal").stat().st_size > 0
        code, report = run_cli(monkeypatch, capsys, backup)
        assert code == 1 and "snapshot_sidecars" in report["blockers"]
        service = BackupService(tmp_path / "exports", backup)
        zipped = tmp_path / "exports" / service.create_backup().filename
        code, report = run_cli(monkeypatch, capsys, zipped)
        assert code == 0 and report["tables"]["series"] == 2
        assert writer.execute("SELECT count(*) FROM series").fetchone() == (2,)


def test_refuses_oversized_zip_directory(backup, tmp_path, monkeypatch, capsys):
    exported = (
        tmp_path / "exports" / BackupService(tmp_path / "exports", backup).create_backup().filename
    )
    altered = tmp_path / "padded.zip"
    with ZipFile(exported) as original, ZipFile(altered, "w") as padded:
        for entry in original.infolist():
            content = original.read(entry)
            entry.comment = b"x" * 65535
            padded.writestr(entry, content)
    code, report = run_cli(monkeypatch, capsys, altered)
    assert code == 1 and "invalid_backup_archive" in report["blockers"]


def test_backup_replaced_during_inspection_is_not_approved(backup, monkeypatch, capsys):
    from pullbox.services import database_migration_preflight as preflight

    inspect = preflight.inspect_snapshot_connection

    def replace_after_read(*args):
        inspect(*args)
        replacement = backup.with_name("other.db")
        shutil.copyfile(backup, replacement)
        os.replace(replacement, backup)

    monkeypatch.setattr(preflight, "inspect_snapshot_connection", replace_after_read)
    code, report = run_cli(monkeypatch, capsys, backup)
    assert code == 1 and "backup_changed" in report["blockers"]


@pytest.mark.parametrize(
    ("state", "revision", "blocked"),
    [
        ("discovered", 0, False),
        ("discovered", 1, True),
        ("queued", 1, True),
        ("intervention", 1, False),
    ],
)
def test_discovery_is_not_a_running_download(backup, state, revision, blocked, monkeypatch, capsys):
    with closing(sqlite3.connect(backup)) as db, db:
        issue = db.execute(
            "INSERT INTO issues(series_id,issue_number,status) SELECT id,1,'WANTED' FROM series"
        ).lastrowid
        db.execute(
            "INSERT INTO direct_acquisition_attempts"
            "(issue_id,request_key,provider_identity,provider_candidate_id,state,plan_revision)"
            " VALUES(?,'r','provider','candidate',?,?)",
            (issue, state, revision),
        )
    code, report = run_cli(monkeypatch, capsys, backup)
    assert code == int(blocked)
    assert ("direct_acquisition_attempts" in report["blockers"]) == blocked
    assert report["tables"]["direct_acquisition_attempts"] == 1
