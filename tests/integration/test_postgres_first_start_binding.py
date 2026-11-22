"""Prove possession of the reviewed target's token without admitting an app session."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import pullbox.app as app
from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages
from tests.integration.test_postgres_first_start import docker

backup = stages.backup
populated = review.populated


@pytest.fixture(scope="module", params=[16, 18])
def named_server(request):
    with targets.disposable_server(request.param) as value:
        yield value


def packaged_probe(image, postgres, url, token, digest):
    # Use the disposable database's loopback, not a development or host service.
    inside = make_url(url).set(port=5432).render_as_string(hide_password=False)
    result = docker(
        "run",
        "--rm",
        "--pull=never",
        "--network",
        f"container:{postgres}",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--mount",
        f"type=bind,src={stages.ROOT / 'src'},dst=/candidate,readonly",
        "--mount",
        f"type=bind,src={token},dst=/run/secrets/first-start,readonly",
        "-e",
        "PULLBOX_DB_URL",
        "--entrypoint=python",
        image,
        "-Bc",
        "import runpy,sys; sys.path.insert(0,'/candidate'); "
        "runpy.run_module('pullbox.cli',run_name='__main__')",
        "migration-check-first-start",
        "--token-file",
        "/run/secrets/first-start",
        "--snapshot-sha256",
        digest,
        env={**os.environ, "PULLBOX_DB_URL": inside},
    )
    assert result.returncode == 0, "Packaged deployment binding check failed"
    assert json.loads(result.stdout)["first_start_binding_verified"]


def probe(url, token, digest):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pullbox.cli",
            "migration-check-first-start",
            "--token-file",
            str(token),
            "--snapshot-sha256",
            digest,
        ],
        cwd=stages.ROOT,
        env={**os.environ, "PULLBOX_DB_URL": url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not result.stderr
    assert url not in result.stdout and token.read_text().strip() not in result.stdout
    report = json.loads(result.stdout)
    assert report["cutover_ready"] is False
    assert result.returncode == int(not report["first_start_binding_verified"])
    return report


async def test_probe_checks_sealed_target_from_intended_configuration(
    named_server, populated, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    from scripts import stage_postgres_transfer as staging

    postgres, server = named_server
    db, url, sql = stages.target(server)
    token = tmp_path / "operator-secret"
    secret = secrets.token_hex(32)
    token.write_text(secret + "\n")
    token.chmod(0o400)
    digest = hashlib.sha256(populated.read_bytes()).hexdigest()
    review.hook_terminal(monkeypatch, ["test-passphrase"], ["confirm"])
    report = {}
    await staging.stage(populated, url, report, review=True, token_file=token)
    assert not report.get("blockers") and report["first_start_bound"]
    marker = stages.state(sql, db)
    assert marker.startswith("false:pullbox:offline-stage:bound-v1:")
    assert secret not in marker + json.dumps(report)
    assert probe(url, token, digest)["first_start_binding_verified"]
    if image := os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE"):
        packaged_probe(image, postgres, url, token, digest)
    assert not probe(url, token, "0" * 64)["first_start_binding_verified"]
    other = tmp_path / "different-secret"
    other.write_text(secrets.token_hex(32))
    other.chmod(0o400)
    assert not probe(url, other, digest)["first_start_binding_verified"]
    # Copying the public database label to a different database is not authority.
    wrong, wrong_url, _ = stages.target(server)
    sql("postgres", f"COMMENT ON DATABASE {wrong} IS '{marker.split(':', 1)[1]}'")
    sql("postgres", f"ALTER DATABASE {wrong} ALLOW_CONNECTIONS false")
    assert not probe(wrong_url, token, digest)["first_start_binding_verified"]
    assert stages.state(sql, db) == marker
    stages.bootstrap_is_blocked(url)
    # TEMPLATE does not inherit the database comment. The internal fence must survive.
    sql("postgres", f"CREATE DATABASE trial OWNER probe_owner TEMPLATE {db}")
    clone = url.rsplit("/", 1)[0] + "/trial"
    targets._migrate(clone, "upgrade", "head", expected_error="Offline migration stage")
    sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS true")
    assert not probe(url, token, digest)["first_start_binding_verified"]
    targets._migrate(url, "upgrade", "head", expected_error="Offline migration stage")
    engine = create_async_engine(clone)
    monkeypatch.setattr(app, "get_engine", lambda: engine)
    monkeypatch.setattr(app, "get_runtime_settings", lambda: SimpleNamespace(db_url=clone))

    async def unexpected_start():
        pytest.fail("The app started normal startup work on an unapproved database")

    monkeypatch.setattr(app, "_run_dev_auto_migrations_if_enabled", unexpected_start)
    try:
        with pytest.raises(RuntimeError, match="Offline migration stage"):
            async with app.lifespan(FastAPI()):
                pytest.fail("The cloned stage served traffic")
    finally:
        await engine.dispose()


async def test_cancelled_review_never_binds(named_server, populated, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    from scripts import stage_postgres_transfer as staging

    _, server = named_server
    db, url, sql = stages.target(server)
    token = tmp_path / "operator-secret"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o400)
    review.hook_terminal(monkeypatch, ["test-passphrase"], ["quit"])
    report = {}
    await staging.stage(populated, url, report, review=True, token_file=token)
    assert report["blockers"] and not report.get("first_start_bound")
    assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
