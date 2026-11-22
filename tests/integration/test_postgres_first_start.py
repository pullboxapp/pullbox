"""Exercise real startup and authenticated navigation on an isolated staging clone."""

import hashlib
import os
import shutil
import subprocess
from contextlib import ExitStack
from uuid import uuid4

import pytest

from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages
from tests.integration.test_sqlite_source_snapshot import inspect

backup = stages.backup
populated = review.populated


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], text=True, capture_output=True, timeout=100, **kwargs)


_NAVIGATE = """
import json, sys, time
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from urllib.parse import urlencode
cookies = SimpleCookie()
csrf = {}
def request(path, form=None, *, payload=None, method=None):
    conn = HTTPConnection('127.0.0.1', 8585, timeout=8)
    try:
        data = json.dumps(payload) if payload is not None else urlencode(form) if form else None
        conn.request(method or ('POST' if data is not None else 'GET'), path, data,
                     {'Content-Type':'application/json' if payload is not None
                                     else 'application/x-www-form-urlencoded',
                      'Origin':'http://127.0.0.1:8585',
                      **(csrf if payload is not None else {}),
                      'Cookie': '; '.join(m.OutputString(attrs=[]) for m in cookies.values())})
        response = conn.getresponse()
        for key, value in response.getheaders():
            if key.lower() == 'set-cookie':
                cookies.load(value)
        return response.status, response.read().decode()
    finally:
        conn.close()
for _ in range(160):
    try:
        if request('/ping')[0] == 200:
            break
    except OSError:
        pass
    time.sleep(0.25)
else:
    sys.exit('Pullbox did not become ready')
status, _ = request('/login', {'username':'reader','password':'test-passphrase'})
assert status == 303 and cookies, ('login', status)
status, body = request('/api/v1/system/about')
assert status == 200, ('about', status)
info = json.loads(body)
assert info['database'].split()[0] == 'PostgreSQL'
assert info['database'].split()[1].split('.')[0] == sys.argv[1] and info['database_path'] is None
for url, expected in [('/issues/83','Private title'),('/story-arcs/55','Private arc'),
                      ('/system?tab=backup','pg_restore')]:
    status, body = request(url)
    assert status == 200 and expected in body, (url, status)
"""


@pytest.mark.parametrize("major", [16, 18])
async def test_first_start_after_review(major, populated, tmp_path, monkeypatch):
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE")
    if not image or os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("requires disposable Docker and an explicit local Pullbox runtime image")
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    from scripts import stage_postgres_transfer as staging

    name = "pullbox-first-start-" + uuid4().hex
    source, network, app = name + "-source", name + "-net", name + "-web"
    package_path = docker(
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--entrypoint=python",
        image,
        "-c",
        "import os; print(os.environ.get('PYTHONPATH', ''))",
        check=True,
    ).stdout.strip()
    data = tmp_path / "source"
    data.mkdir()
    (data / "comics").mkdir()
    live = data / "pullbox.db"
    shutil.copyfile(populated, live)
    before = hashlib.sha256(live.read_bytes()).digest()
    with ExitStack() as cleanup:
        cleanup.callback(docker, "rm", "-fv", source)
        docker(
            "run",
            "--name",
            source,
            "--network=none",
            "--pull=never",
            "--mount",
            f"type=bind,src={data},dst=/data",
            "-e",
            "PULLBOX_LIBRARY_ROOT=/data/comics",
            "-e",
            "PULLBOX_DB_URL=sqlite+aiosqlite:////data/pullbox.db",
            "--entrypoint=python",
            image,
            "-c",
            "pass",
            check=True,
        )
        offline = inspect(source)
        assert not offline["blockers"], offline["blockers"]
        checkpoint = ["--compare-checkpoint", offline["checkpoint_sha256"]]
        snapshot = tmp_path / "offline.db"
        captured = inspect(source, *checkpoint, "--snapshot", str(snapshot))
        assert captured["snapshot_verified"], captured["blockers"]
        postgres, server = cleanup.enter_context(targets.disposable_server(major))
        db, url, sql = stages.target(server)
        review.hook_terminal(monkeypatch, ["test-passphrase"], ["confirm"])
        report = {}
        await staging.stage(snapshot, url, report, review=True)
        assert report["snapshot_sha256"] == captured["snapshot_sha256"]
        assert report["maintenance_review"]["status"] == "confirmed"
        assert report["maintenance_review"]["follow_up_jobs"] == 1
        assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
        assert inspect(source, *checkpoint)["checkpoint_matched"]
        # Rehearsal only: create a running sibling in this disposable cluster.
        # Never reopen the verified stage or mount the source data in the new app.
        sql("postgres", f"CREATE DATABASE trial OWNER probe_owner TEMPLATE {db}")
        docker("network", "create", "--internal", network, check=True)
        cleanup.callback(docker, "network", "rm", network)
        docker("network", "connect", "--alias", "trial-db", network, postgres, check=True)
        cleanup.callback(docker, "network", "disconnect", network, postgres)
        cleanup.callback(docker, "rm", "-fv", app)
        env = {
            **os.environ,
            "PULLBOX_DB_URL": f"postgresql+asyncpg://probe_owner:{server[1]}@trial-db/trial",
        }
        docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            app,
            "--network",
            network,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/data:uid=65532,gid=65532,mode=0700",
            "--tmpfs",
            "/comics:uid=65532,gid=65532,mode=0700",
            "--mount",
            f"type=bind,src={stages.ROOT / 'src'},dst=/candidate/src,readonly",
            "--mount",
            f"type=bind,src={stages.ROOT / 'alembic'},dst=/app/alembic,readonly",
            "-e",
            f"PYTHONPATH=/candidate/src:{package_path}",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PULLBOX_DB_URL",
            image,
            env=env,
            check=True,
        )
        result = docker("exec", "-i", app, "python", "-", str(major), input=_NAVIGATE)
        logs = docker("logs", app, check=True)
        log = logs.stdout + logs.stderr
        if server[1] in log:
            pytest.fail("Database credential in container log")
        if result.returncode:
            errors = [
                line[-1300:]
                for line in log.splitlines()
                if "Error:" in line or "Exception in ASGI" in line or "request_failed" in line
            ]
            pytest.fail(result.stderr + "\n" + "\n".join(errors[-15:]))
        assert "[ready] Scheduler: active" in log
        assert sql("trial", "SELECT status FROM issues WHERE id=83") == "OWNED"
        assert sql("trial", "SELECT status FROM import_files WHERE id=2") == "FAILED"
        assert sql("trial", "SELECT count(*) FROM issue_story_arcs WHERE issue_id=83") == "1"
        assert inspect(source, *checkpoint)["checkpoint_matched"]
        assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
        assert hashlib.sha256(live.read_bytes()).digest() == before
