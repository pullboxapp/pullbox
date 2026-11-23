"""An attested stage and the still-stopped source must describe the same capture."""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from tests.integration import postgres_import_workload as workload
from tests.integration import postgres_linked_catalog as linked
from tests.integration import postgres_manual_metadata as manual
from tests.integration import postgres_native_followup as native
from tests.integration import postgres_propagation as propagation
from tests.integration import postgres_series_editor as series_editor
from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages
from tests.integration.test_postgres_first_start import _NAVIGATE, docker
from tests.integration.test_sqlite_source_snapshot import inspect

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


@pytest.fixture(scope="module", params=[16, 18])
def cluster(request):
    from tests.integration.test_database_migration_target import disposable_server

    with disposable_server(
        request.param, publish_app=os.environ.get("PULLBOX_TEST_MIGRATION_BROWSER") == "1"
    ) as instance:
        yield instance


@pytest.mark.parametrize(
    "start",
    [None, "activate", "restart", "browser"],
    ids=["check", "activate", "restart", "browser"],
)
async def test_recheck_original_source_and_reviewed_target(
    cluster, populated, tmp_path, monkeypatch, start
):
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE")
    if not image:
        pytest.skip("requires the disposable source image")
    browser = start == "browser"
    if browser and os.environ.get("PULLBOX_TEST_MIGRATION_BROWSER") != "1":
        pytest.skip("opt in to the disposable Chromium cutover rehearsal")
    activated = start in {"activate", "browser"}
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
    postgres, server = cluster
    comics = data / "comics"
    covers = comics / ".covers"
    covers.mkdir()
    (covers / "71").mkdir()
    (covers / "71" / "series.jpg").write_bytes(b"original cover")
    (comics / "one.cbz").write_bytes(b"first")
    imports = tmp_path / "imports"
    expected = None
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO library_roots(id,path,name,enabled) VALUES(77,'/comics','main',1)")
        db.execute(
            "INSERT INTO library_files(id,library_root_id,file_path,file_name,file_size,"
            "file_format,file_modified_at,match_confidence,has_comicinfo) VALUES "
            "(101,77,'/comics/one.cbz','one.cbz',5,'CBZ',CURRENT_TIMESTAMP,'UNMATCHED',0)"
        )
        if activated:
            expected = workload.seed(db, comics, imports)
        if browser:
            owned_digest = linked.seed(db, imports, comics)
            manual.seed(db, imports)
            propagation.seed(db, comics)
            db.execute(
                "INSERT INTO series_external_identities(series_id,identity_namespace,external_id,"
                "verification_state,evidence_kind) "
                "VALUES(1001,'comicvine','92000','verified','provider_result')"
            )
    originals = native.make_sources(imports) if browser else {}
    token = tmp_path / "token"
    key = secrets.token_hex(32)
    xml = server[0] == 18
    key_file = "config.xml" if xml else "application-key"

    def key_contents(value):
        return f"<Config><SecretKey>{value}</SecretKey></Config>" if xml else value

    if xml:
        (data / key_file).write_text(key_contents(key))
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

    try:
        source_run = docker(
            "run",
            "--name",
            name,
            "--network=none",
            "--pull=never",
            "--mount",
            f"type=bind,src={data},dst=/data",
            "--mount",
            f"type=bind,src={comics},dst=/comics",
            *(
                [
                    "--mount",
                    f"type=bind,src={imports},dst=/imports,readonly",
                    "--mount",
                    f"type=bind,src={stages.ROOT / 'src'},dst=/candidate/src,readonly",
                    "--mount",
                    f"type=bind,src={native.__file__},dst=/scan.py,readonly",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                ]
                if browser
                else []
            ),
            "-e",
            "PULLBOX_DB_URL=sqlite:////data/pullbox.db",
            "-e",
            "PULLBOX_LIBRARY_ROOT=/comics",
            "-e",
            "PULLBOX_SECRET_KEY",
            "--entrypoint=python",
            image,
            "-c",
            "import sys,runpy; sys.path.insert(0,'/candidate/src'); "
            "runpy.run_path('/scan.py',run_name='__main__')"
            if browser
            else "pass",
            env={**os.environ, "PULLBOX_SECRET_KEY": "" if xml else key},
            check=True,
        )
        cases = json.loads(source_run.stdout.splitlines()[-1]) if browser else []
        if browser:
            series_editor.seed_sidecar(comics)
        write_wal("first confidential title")
        before = hashlib.sha256(source.read_bytes()).digest()
        offline = inspect(name)
        assert not offline["blockers"], offline["blockers"]
        checkpoint = offline["checkpoint_sha256"]
        captured = inspect(name, "--compare-checkpoint", checkpoint, "--snapshot", str(snapshot))
        assert captured["snapshot_verified"], captured["blockers"]
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
        recovered = tmp_path / "restored.db"
        shutil.copyfile(snapshot, recovered)
        settings = tmp_path / "settings"
        settings.mkdir(mode=0o700)
        (settings / key_file).write_text(key_contents(key))
        (settings / "compose.yaml").write_text("services: {pullbox: {image: local-test}}")
        restored_settings = tmp_path / "restored-settings"
        shutil.copytree(settings, restored_settings)
        approval = scratch / "approval.json"
        options = dict(
            original_backup=snapshot,
            restored_backup=recovered,
            original_settings=settings,
            restored_settings=restored_settings,
            key_source="config-xml" if xml else "secret-file",
            deployment_file="compose.yaml",
            output=approval,
        )
        assert "cancelled" in await host.approve(
            name, captured_source, scratch, url, token, confirm=lambda _: False, **options
        )
        assert not approval.exists()
        assert "independent_backup" in await host.approve(
            name,
            captured_source,
            scratch,
            url,
            token,
            confirm=lambda _: True,
            **{**options, "restored_backup": snapshot},
        )
        for directory in (settings, restored_settings):
            (directory / key_file).write_text(key_contents("bc32" * 16))
        assert "original_key" in await host.approve(
            name, captured_source, scratch, url, token, confirm=lambda _: True, **options
        )
        assert not approval.exists()
        for directory in (settings, restored_settings):
            (directory / key_file).write_text(key_contents(key))
        assert not await host.approve(
            name, captured_source, scratch, url, token, confirm=lambda _: True, **options
        )
        assert approval.stat().st_mode & 0o777 == 0o600
        assert key not in approval.read_text() and token.read_text() not in approval.read_text()

        if start:
            app = name + "-first-start"
            inside = make_url(url).set(port=5432).render_as_string(hide_password=False)
            shared = xml and activated
            copied = comics if shared else tmp_path / "new-library"
            if not shared:
                shutil.copytree(comics, copied)
            packages = docker(
                "run",
                "--rm",
                "--network=none",
                "--pull=never",
                "--entrypoint=python",
                image,
                "-c",
                "import os;print(os.getenv('PYTHONPATH',''))",
                check=True,
            ).stdout.strip()
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--name",
                app,
                "-i",
                "--pull=never",
                "--network",
                f"container:{postgres}",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cap-drop=ALL",
                "--tmpfs",
                f"/data:uid={os.getuid()},gid={os.getgid()},mode=0700",
                "--mount",
                f"type=bind,src={stages.ROOT / 'src'},dst=/candidate/src,readonly",
                "--mount",
                f"type=bind,src={stages.ROOT / 'alembic'},dst=/app/alembic,readonly",
                "--mount",
                f"type=bind,src={copied},dst=/comics",
                *(
                    ["--mount", f"type=bind,src={imports},dst=/imports,readonly"]
                    if expected
                    else []
                ),
                "--mount",
                f"type=bind,src={token},dst=/run/token,readonly",
                "--mount",
                f"type=bind,src={approval},dst=/run/approval,readonly",
                "--mount",
                f"type=bind,src={recovered},dst=/run/backup.db,readonly",
                "-e",
                f"PYTHONPATH=/candidate/src:{packages}",
                "-e",
                "PULLBOX_SECRET_KEY",
                "-e",
                "PULLBOX_DB_URL",
                "--entrypoint=python",
                image,
                "-Bm",
                "pullbox.cli",
                "migration-start",
                "--token-file",
                "/run/token",
                "--approval-file",
                "/run/approval",
                "--restored-backup",
                "/run/backup.db",
                "--snapshot-sha256",
                captured_source.snapshot,
                env={**os.environ, "PULLBOX_DB_URL": inside, "PULLBOX_SECRET_KEY": key},
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            try:
                line = (await asyncio.wait_for(process.stdout.readline(), 70)).decode()
                assert "First-start challenge: " in line, line
                initial = docker("logs", app, check=True)
                assert "[ready] Scheduler: active" not in initial.stdout + initial.stderr
                assert stages.state(sql, db) == original_stage
                challenge = line.split()[-1]
                args = (name, captured_source, scratch, url, token, challenge)
                assert not (await host.authorize_start(*args, lambda _: False))[1]
                if start == "restart":
                    # A restart after issuing approval invalidates the live grant.
                    docker("start", "--attach", name, check=True)
                    errors, response = await host.authorize_start(*args, lambda _: True)
                    assert "checkpoint_changed" in errors and response is None
                    docker("stop", "-t", "1", app, check=True)
                    await asyncio.wait_for(process.wait(), 20)
                    assert stages.state(sql, db) == original_stage
                    return
                if shared:
                    errors, response = await host.authorize_start(*args, lambda _: True)
                    assert "other_writable_container" in errors and not response
                    peer = name + "-unrelated-writer"
                    try:
                        docker(
                            "run",
                            "-d",
                            "--name",
                            peer,
                            "--network=none",
                            "--pull=never",
                            "--mount",
                            f"type=bind,src={comics},dst=/unrelated",
                            "--entrypoint=python",
                            image,
                            "-c",
                            "import time;time.sleep(90)",
                            check=True,
                        )
                        errors, response = await host.authorize_start(
                            *args, lambda _: True, candidate=app
                        )
                        assert "other_writable_container" in errors and not response
                        errors, response = await host.authorize_start(
                            *args, lambda _: True, candidate=peer
                        )
                        assert "candidate_not_waiting" in errors and not response
                    finally:
                        docker("rm", "-fv", peer)
                    errors, response = await host.authorize_start(
                        *args, lambda _: True, candidate=app
                    )
                else:
                    errors, response = await host.authorize_start(*args, lambda _: True)
                assert response and not errors, errors
                result = await asyncio.to_thread(
                    docker,
                    "exec",
                    "-i",
                    app,
                    "python",
                    "-",
                    str(server[0]),
                    input=_NAVIGATE.replace("Private title", "first confidential title")
                    + (workload.OPERATOR_ACTIONS if expected else ""),
                )
                if result.returncode:
                    logs = docker("logs", app)
                    safe = (
                        (logs.stderr + logs.stdout)
                        .replace(server[1], "[redacted]")
                        .replace(key, "[redacted]")
                    )
                    pytest.fail(result.stderr + safe[-3000:])
                assert stages.state(sql, db) == "true:"
                started = docker("logs", app, check=True)
                assert "[ready] Scheduler: active" in started.stdout + started.stderr
                assert sql(db, "SELECT status FROM issues WHERE id=83") == "OWNED"
                assert sql(db, "SELECT status FROM import_files WHERE id=2") == "FAILED"
                if expected:
                    workload.verify(
                        sql,
                        db,
                        copied,
                        imports,
                        expected,
                        mylar_neighbor=browser,
                        extra_files=propagation.FILES if browser else (),
                    )
                if cases:
                    address = docker("port", postgres, "8585/tcp", check=True).stdout.strip()
                    assert address.startswith("127.0.0.1:")
                    refreshed_rows = await native.exercise(
                        "http://" + address, cases, originals, copied, db, sql, owned_digest
                    )
                if not shared:
                    assert inspect(name, "--compare-checkpoint", checkpoint)["checkpoint_matched"]
                assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == captured_source.snapshot
                # A native backup of the now-live original stage must be usable without its
                # first-start secrets and must retain writes made after SQLite was frozen.
                docker("stop", "-t", "12", app, check=True)
                if cases:
                    stat_query = (
                        "SELECT last_execution,last_status FROM scheduled_task_stats "
                        "WHERE task_id='monitor_downloads' AND last_execution IS NOT NULL"
                    )
                    last_run = sql(db, stat_query)
                    assert last_run and last_run.split("|")[1]
                sql(db, "UPDATE story_arcs SET name='post-cutover change' WHERE id=55")
                restored, restore_url, _ = stages.target(server)
                commands = ["docker", "exec", "-i", postgres]
                with (tmp_path / "after-start.dump").open("w+b") as dump:
                    subprocess.run(
                        [*commands, "pg_dump", "-Fc", "-U", "probe_owner", "-d", db],
                        stdout=dump,
                        check=True,
                        timeout=60,
                    )
                    dump.seek(0)
                    subprocess.run(
                        [
                            *commands,
                            "pg_restore",
                            "--single-transaction",
                            "--exit-on-error",
                            "--no-owner",
                            "--no-acl",
                            "-U",
                            "probe_owner",
                            "-d",
                            restored,
                        ],
                        stdin=dump,
                        check=True,
                        timeout=60,
                    )
                stages.targets._migrate(restore_url, "upgrade", "head")
                if expected:
                    workload.verify(
                        sql,
                        restored,
                        copied,
                        imports,
                        expected,
                        mylar_neighbor=browser,
                        linked_additions=2 if browser else 0,
                        extra_files=propagation.FILES if browser else (),
                    )
                if cases:
                    assert linked.verify(sql, restored) == refreshed_rows
                    manual.verify(sql, restored, refreshed=True)
                    series_editor.verify(sql, restored, copied)
                    propagation.verify(sql, restored, copied)
                    assert sql(restored, stat_query) == last_run
                assert (
                    sql(restored, "SELECT name FROM story_arcs WHERE id=55")
                    == "post-cutover change"
                )
            finally:
                docker("rm", "-fv", app)
                await asyncio.wait_for(process.wait(), 20)
            return

        def deployment(secret, media, *, readonly=True):
            inside = make_url(url).set(port=5432).render_as_string(hide_password=False)
            run = docker(
                "run",
                "--rm",
                "--pull=never",
                "--network",
                f"container:{postgres}",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--read-only",
                "--cap-drop=ALL",
                "--mount",
                f"type=bind,src={stages.ROOT / 'src'},dst=/candidate,readonly",
                "--mount",
                f"type=bind,src={token},dst=/run/secrets/token,readonly",
                "--mount",
                f"type=bind,src={approval},dst=/run/approval,readonly",
                "--mount",
                f"type=bind,src={recovered},dst=/run/backup.db" + (",readonly" if readonly else ""),
                "--mount",
                f"type=bind,src={media},dst=/comics,readonly",
                "-e",
                "PULLBOX_DB_URL",
                "-e",
                "PULLBOX_SECRET_KEY",
                "--entrypoint=python",
                image,
                "-Bc",
                "import sys,runpy;sys.path.insert(0,'/candidate');"
                "runpy.run_module('pullbox.cli',run_name='__main__')",
                "migration-check-first-start",
                "--token-file",
                "/run/secrets/token",
                "--snapshot-sha256",
                captured_source.snapshot,
                "--approval-file",
                "/run/approval",
                "--restored-backup",
                "/run/backup.db",
                env={**os.environ, "PULLBOX_DB_URL": inside, "PULLBOX_SECRET_KEY": secret},
            )
            assert run.returncode in (0, 1) and secret not in run.stdout + run.stderr
            result = json.loads(run.stdout)
            assert run.returncode == int(not result["deployment_approval_verified"])
            return result

        assert deployment(key, comics)["deployment_approval_verified"]
        assert not deployment(secrets.token_hex(32), comics)["deployment_approval_verified"]
        wrong = tmp_path / "other-comics"
        shutil.copytree(comics, wrong)
        (wrong / "one.cbz").write_bytes(b"wrong")
        assert not deployment(key, wrong)["deployment_approval_verified"]
        shutil.copy2(comics / "one.cbz", wrong / "one.cbz")
        (wrong / ".covers" / "71" / "series.jpg").write_bytes(b"replaced cover")
        assert not deployment(key, wrong)["deployment_approval_verified"]
        assert not deployment(key, comics, readonly=False)["deployment_approval_verified"]
        approval.unlink()
        assert stages.state(sql, db) == original_stage
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
