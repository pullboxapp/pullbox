"""Exercise the operator's actual source checkpoint and capture workflow."""

import errno
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_sqlite_source_offline.py"


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=120, check=True).stdout


def inspect(name, *args):
    run = subprocess.run(
        [sys.executable, SCRIPT, "--container", name, *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert run.returncode in (0, 1), run.stderr
    assert not run.stderr
    assert "confidential" not in run.stdout
    report = json.loads(run.stdout)
    assert not report["cutover_ready"]
    assert (run.returncode == 0) == (not report["blockers"])
    return report


@pytest.fixture
def sqlite_source(tmp_path):
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE")
    if not image or os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("requires an explicitly selected local Pullbox image and disposable Docker")
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "comics").mkdir()
    db = folder / "pullbox.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
        env={**os.environ, "PULLBOX_DB_URL": f"sqlite+aiosqlite:///{db}"},
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    # Leave a real committed WAL frame on disk without an active host connection.
    command(
        sys.executable,
        "-c",
        """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
          "VALUES(789,'confidential','s','CONTINUING',1,1)")
c.commit()
os._exit(0)
""",
        str(db),
    )
    assert (folder / "pullbox.db-wal").stat().st_size > 32
    with closing(sqlite3.connect(db.as_uri() + "?immutable=1", uri=True)) as c:
        assert c.execute("SELECT COUNT(*) FROM series WHERE id=789").fetchone() == (0,)
    for path in folder.glob("pullbox.db*"):
        path.chmod(0o644)
    return image, folder


def test_wal_snapshot_from_stopped_app_is_private_verified_and_no_clobber(tmp_path, sqlite_source):
    image, folder = sqlite_source
    db = folder / "pullbox.db"

    def source_state():
        return {
            p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in folder.glob("pullbox.db*")
        }

    original = source_state()
    name = "pullbox-snapshot-test-" + uuid4().hex
    snap = tmp_path / "result.db"
    command(
        "docker",
        "run",
        "--name",
        name,
        "--network=none",
        "--pull=never",
        "--mount",
        f"type=bind,src={folder},dst=/data",
        "-e",
        "PULLBOX_DB_URL=sqlite+aiosqlite:////data/pullbox.db",
        "-e",
        "PULLBOX_LIBRARY_ROOT=/data/comics",
        "--entrypoint=python",
        image,
        "-c",
        "pass",
    )
    try:
        first = inspect(name)
        assert first["offline_checks_passed"], first
        previous = ["--compare-checkpoint", first["checkpoint_sha256"]]
        saved = inspect(name, *previous, "--snapshot", str(snap))
        assert not saved["blockers"], saved
        assert saved.get("snapshot_sha256") == hashlib.sha256(snap.read_bytes()).hexdigest(), saved
        assert saved["checkpoint_matched"] and saved["snapshot_verified"]
        assert snap.stat().st_mode & 0o777 == 0o600
        with closing(sqlite3.connect(snap.as_uri() + "?immutable=1", uri=True)) as c:
            assert c.execute("SELECT title FROM series WHERE id=789").fetchone() == (
                "confidential",
            )
        assert not list(tmp_path.glob("result.db-*"))
        for major in ("16", "18"):
            rehearsed = json.loads(
                command(
                    sys.executable,
                    str(ROOT / "scripts/rehearse_postgres_transfer.py"),
                    "--backup",
                    str(snap),
                    "--postgres-major",
                    major,
                )
            )
            assert rehearsed["rehearsal_passed"] and not rehearsed["cutover_ready"], rehearsed
            assert rehearsed["verified_tables"]["series"] == 1
        assert (
            "snapshot_destination" in inspect(name, *previous, "--snapshot", str(snap))["blockers"]
        )
        assert (
            "snapshot_destination"
            in inspect(name, *previous, "--snapshot", str(folder / "new"))["blockers"]
        )
        assert not (folder / "new").exists()
        assert source_state() == original
        with closing(sqlite3.connect(db)) as c, c:
            c.execute(
                "INSERT INTO utility_jobs(id,job_type,state,display_name) "
                "VALUES('t','file_convert','CANCELLING','confidential')"
            )
        refused = inspect(name, *previous, "--snapshot", str(tmp_path / "unsafe.db"))
        assert "utility_jobs" in refused["blockers"] and not (tmp_path / "unsafe.db").exists()
    finally:
        command("docker", "rm", "-v", name)


def test_named_volume_without_shm_and_real_restart_before_publication(
    sqlite_source, tmp_path, monkeypatch, capsys
):
    image, folder = sqlite_source
    name = "pullbox-volume-test-" + uuid4().hex
    volume = name + "-state"
    command("docker", "volume", "create", volume)
    try:
        command(
            "docker",
            "run",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--mount",
            f"type=volume,src={volume},dst=/data",
            "--mount",
            f"type=bind,src={folder},dst=/fixture,readonly",
            "--user=0:0",
            "--entrypoint=python",
            "-e",
            "PULLBOX_DB_URL=sqlite:////data/pullbox.db",
            "-e",
            "PULLBOX_LIBRARY_ROOT=/data/comics",
            image,
            "-c",
            "from pathlib import Path; import shutil; "
            "first = not Path('/data/pullbox.db').exists(); "
            "[shutil.copyfile('/fixture/'+s, '/data/'+s) for s in "
            "(['pullbox.db','pullbox.db-wal'] if first else [])]; "
            "assert not Path('/data/pullbox.db-shm').exists()",
        )
        previous = inspect(name)["checkpoint_sha256"]
        path = tmp_path / "volume.db"
        ok = inspect(name, "--snapshot", str(path), "--compare-checkpoint", previous)
        assert ok.get("snapshot_verified"), ok
        with closing(sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)) as c:
            assert c.execute("SELECT count(*) FROM series WHERE id=789").fetchone() == (1,)
        monkeypatch.syspath_prepend(str(ROOT / "scripts"))
        host = importlib.import_module("check_sqlite_source_offline")
        helper = importlib.import_module("sqlite_source_snapshot")
        worker = helper.run_worker

        def restart(*args):
            result = worker(*args)
            if args[-1] == "check":
                command("docker", "start", "--attach", name)
            return result

        monkeypatch.setattr(helper, "run_worker", restart)
        rejected = tmp_path / "changed.db"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(SCRIPT),
                "--container",
                name,
                "--snapshot",
                str(rejected),
                "--compare-checkpoint",
                previous,
            ],
        )
        assert host.main() == 1
        assert "checkpoint_changed" in json.loads(capsys.readouterr().out)["blockers"]
        assert not rejected.exists()
        assert not list(tmp_path.glob(".pullbox-snapshot-*"))
    finally:
        if command("docker", "ps", "-aq", "--filter", "name=^/" + name + "$").strip():
            command("docker", "rm", "-f", "-v", name)
        command("docker", "volume", "rm", volume)


def test_stop_before_copy_if_scratch_lacks_space(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    worker = importlib.import_module("sqlite_source_snapshot_worker")
    source = tmp_path / "source"
    source.write_bytes(b"x")
    monkeypatch.setattr(worker, "evidence", lambda _: "checked")
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 0})())
    with pytest.raises(OSError) as exc:
        worker.run(source, True)
    assert exc.value.errno == errno.ENOSPC


@pytest.mark.parametrize("name", ["live.db-wal", "live.db-shm", "live.db-journal"])
def test_never_publish_into_another_databases_journal_name(name, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("sqlite_source_snapshot")
    assert module.unsafe_output(tmp_path / name)


@pytest.mark.parametrize("interruption", ["changed_files", "restart", "output_taken", "sidecar"])
def test_no_publication_after_changed_evidence_or_destination(interruption, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("sqlite_source_snapshot")
    path = tmp_path / "new.db"

    def fake_worker(_, __, work, mode):
        if mode == "capture":
            (work / "snapshot").write_bytes(b"fixture")
        return "after" if mode == "check" and interruption == "changed_files" else "before"

    def recheck():
        if interruption == "restart":
            return {"checkpoint_changed": "Source restarted"}
        if interruption == "output_taken":
            path.write_bytes(b"keep this file")
        if interruption == "sidecar":
            path.with_suffix(".db-wal").write_bytes(b"keep this journal")
        return {}

    monkeypatch.setattr(module, "run_worker", fake_worker)
    monkeypatch.setattr(
        module,
        "inspect_migration_backup",
        lambda *a, **kw: SimpleNamespace(source_checks_passed=True),
    )
    result, reasons = module.capture_snapshot(SimpleNamespace(mounts=[]), path, None, recheck, Path)
    assert not result["snapshot_verified"] and result["snapshot_sha256"] is None
    expected = {"changed_files": "source_files_changed", "restart": "checkpoint_changed"}
    assert expected.get(interruption, "snapshot_destination") in reasons
    if interruption == "output_taken":
        assert path.read_bytes() == b"keep this file"
    else:
        assert not path.exists()
    if interruption == "sidecar":
        assert path.with_suffix(".db-wal").read_bytes() == b"keep this journal"
    assert not list(tmp_path.glob(".pullbox-snapshot-*"))
