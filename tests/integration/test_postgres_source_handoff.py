"""An attested stage and the still-stopped source must describe the same capture."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages
from tests.integration.test_postgres_first_start import docker
from tests.integration.test_sqlite_source_snapshot import inspect

server = stages.server
backup = stages.backup
populated = review.populated


def handoff(source, url, token, capture, scratch):
    run = subprocess.run(
        [
            sys.executable,
            "scripts/check_postgres_handoff.py",
            "--container",
            source,
            "--capture-report",
            str(capture),
            "--token-file",
            str(token),
            "--scratch-dir",
            str(scratch),
            "--url-stdin",
        ],
        cwd=stages.ROOT,
        input=url + "\n",
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert not run.stderr
    assert url not in run.stdout and token.read_text() not in run.stdout
    report = json.loads(run.stdout)
    assert run.returncode == int(not report["source_and_target_verified"])
    assert report["cutover_ready"] is False
    assert not list(scratch.iterdir())
    return report


async def test_recheck_original_source_and_reviewed_target(
    server, populated, tmp_path, monkeypatch
):
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE")
    if not image:
        pytest.skip("requires the disposable source image")
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    import check_postgres_handoff as host

    from pullbox.services.migration_first_start import BindingError, check_binding
    from scripts import stage_postgres_transfer as staging

    name = "pullbox-handoff-" + uuid4().hex
    data, scratch = tmp_path / "data", tmp_path / "scratch"
    (data / "comics").mkdir(parents=True)
    scratch.mkdir(mode=0o700)
    source = data / "pullbox.db"
    shutil.copyfile(populated, source)
    token = tmp_path / "token"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o400)
    snapshot, receipt = tmp_path / "snapshot.db", tmp_path / "capture.json"

    # Commit to the WAL but don't let SQLite checkpoint on last-close.
    def write_wal(title):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                "assert db.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',); "
                "db.execute('PRAGMA wal_autocheckpoint=0'); "
                "db.execute('UPDATE series SET title=? WHERE id=71',(sys.argv[2],)); "
                "db.commit(); os._exit(0)",
                str(source),
                title,
            ],
            check=True,
        )

    write_wal("first confidential title")
    before = hashlib.sha256(source.read_bytes()).digest()
    try:
        docker(
            "run",
            "--name",
            name,
            "--network=none",
            "--pull=never",
            "--mount",
            f"type=bind,src={data},dst=/data",
            "-e",
            "PULLBOX_DB_URL=sqlite:////data/pullbox.db",
            "-e",
            "PULLBOX_LIBRARY_ROOT=/data/comics",
            "--entrypoint=python",
            image,
            "-c",
            "pass",
            check=True,
        )
        checkpoint = inspect(name)["checkpoint_sha256"]
        captured = inspect(name, "--compare-checkpoint", checkpoint, "--snapshot", str(snapshot))
        assert captured["snapshot_verified"]
        receipt.write_text(json.dumps(captured))
        db, url, sql = stages.target(server)
        receipt.write_text(json.dumps({**captured, "snapshot_sha256": "0" * 64}))
        with pytest.raises(BindingError, match="backup does not match"):
            await staging.stage(
                snapshot, url, {}, review=True, token_file=token, capture_report=receipt
            )
        assert stages.state(sql, db) == "true:"
        receipt.write_text(json.dumps(captured))
        review.hook_terminal(monkeypatch, ["test-passphrase"], ["confirm"])
        stage = {}
        await staging.stage(
            snapshot, url, stage, review=True, token_file=token, capture_report=receipt
        )
        assert stage["source_capture_bound"]
        original_stage = stages.state(sql, db)
        assert original_stage.startswith("false:")
        await check_binding(url, token, captured["snapshot_sha256"])
        assert handoff(name, url, token, receipt, scratch)["source_and_target_verified"]
        captured_source = host.SourceCapture.read(receipt)
        with monkeypatch.context() as patch:
            compare = host.compare_snapshot

            def open_target_after_comparison(*args):
                outcome = compare(*args)
                assert not outcome[1]
                sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS true")
                return outcome

            patch.setattr(host, "compare_snapshot", open_target_after_comparison)
            assert "target_binding" in await host.verify(name, captured_source, scratch, url, token)
        sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS false")
        assert "snapshot_scratch" in await host.verify(name, captured_source, data, url, token)
        assert not list(data.glob(".pullbox-snapshot-*"))
        receipt.write_text(json.dumps({**captured, "source_file_state_sha256": "0" * 64}))
        assert "target_binding" in handoff(name, url, token, receipt, scratch)["blockers"]
        receipt.write_text(json.dumps(captured))
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns - 1_000_000_000))
        assert "source_files_changed" in handoff(name, url, token, receipt, scratch)["blockers"]
        write_wal("second confidential title")
        assert hashlib.sha256(source.read_bytes()).digest() == before
        assert inspect(name, "--compare-checkpoint", checkpoint)["checkpoint_matched"]
        assert "source_snapshot_mismatch" in handoff(name, url, token, receipt, scratch)["blockers"]
        assert stages.state(sql, db) == original_stage
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == captured["snapshot_sha256"]
        docker("start", "--attach", name, check=True)
        assert "checkpoint_changed" in handoff(name, url, token, receipt, scratch)["blockers"]
    finally:
        docker("rm", "-fv", name)
