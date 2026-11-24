"""Exercise the operator's fresh-install example, never an existing application or DB."""

import json
import os
import secrets
import subprocess
from uuid import uuid4

import pytest

from tests.integration.test_compose_metadata_flags import DOCKER_DIR, FLAGS


@pytest.fixture
def fresh_stack(tmp_path, docker_cli):
    example = DOCKER_DIR / "docker-compose.postgres.yml"
    assert example.is_file(), "No standalone Compose example for a new PostgreSQL library"
    name = "pullbox-fresh-" + uuid4().hex[:12]
    offline = tmp_path / "offline.yml"
    offline.write_text(
        "services:\n  pullbox:\n    environment:\n"
        "      PULLBOX_STARTUP_UPDATE_CHECK_ENABLED: 'false'\n"
        "      PULLBOX_DATA_API_BASE_URL: http://127.0.0.1:1\n"
        "      HTTPS_PROXY: http://127.0.0.1:1\n      HTTP_PROXY: http://127.0.0.1:1\n"
        "      NO_PROXY: db,localhost,127.0.0.1\n"
    )
    admin, password = secrets.token_hex(24), secrets.token_hex(24)
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE", "pullbox-unused:render-only")

    def compose(*args, major=18, stdin=None):
        run = subprocess.run(
            [
                docker_cli,
                "compose",
                "-p",
                name,
                "--env-file",
                os.devnull,
                "-f",
                str(example),
                "-f",
                str(offline),
                *args,
            ],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "HOME": os.environ["HOME"],
                "PULLBOX_IMAGE": image,
                "PULLBOX_PG_IMAGE": f"postgres:{major}-bookworm",
                **({"PULLBOX_PG_VOLUME_ROOT": "/var/lib/postgresql/data"} if major == 16 else {}),
                "PULLBOX_PG_HOST_PORT": "0",
                "PULLBOX_PG_ADMIN_PASSWORD": admin,
                "PULLBOX_PG_APP_PASSWORD": password,
            },
            input=stdin,
            capture_output=True,
            text=True,
            timeout=150,
        )
        if run.returncode:
            pytest.fail(
                run.stderr.replace(password, "[redacted]").replace(admin, "[redacted]")[-1400:],
                pytrace=False,
            )
        return run.stdout.strip()

    return compose, password


@pytest.mark.parametrize("major", [16, 18])
def test_fresh_example_is_separate_private_and_least_privilege(fresh_stack, major):
    compose, _ = fresh_stack
    spec = json.loads(compose("config", "--format", "json", major=major))
    app, db = (spec["services"][key] for key in ("pullbox", "db"))
    assert app["environment"]["PULLBOX_DB_URL"] == "postgresql+asyncpg://pullbox@db:5432/pullbox"
    assert "POSTGRES_PASSWORD" not in app["environment"]
    assert app["environment"]["PGPASSWORD"] != db["environment"]["POSTGRES_PASSWORD"]
    assert not db.get("ports")
    image_volume = "/var/lib/postgresql/data" if major == 16 else "/var/lib/postgresql"
    assert [v["target"] for v in db["volumes"]] == [image_volume]
    assert db["environment"]["PGDATA"] == image_volume + "/pullbox/cluster"
    assert app["ports"][0]["host_ip"] == "127.0.0.1"
    assert app["depends_on"]["db"]["condition"] == "service_healthy"
    assert all(app["environment"][flag] == "false" for flag in FLAGS)
    assert all(v["type"] == "volume" for v in app["volumes"] + db["volumes"])
    assert not any(v.get("external") for v in spec["volumes"].values())
    assert all(v["name"].startswith(spec["name"] + "_") for v in spec["volumes"].values())


@pytest.mark.parametrize("major", [16, 18])
def test_new_account_survives_recreating_both_containers(fresh_stack, major):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1" or not os.environ.get(
        "PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE"
    ):
        pytest.skip("Requires disposable PostgreSQL and a local Pullbox image")
    from playwright.sync_api import expect, sync_playwright

    from tests.e2e.pages.login import LoginPage
    from tests.e2e.pages.setup_wizard import SetupWizardPage

    compose, password = fresh_stack

    def run(*args, stdin=None):
        return compose(*args, major=major, stdin=stdin)

    def sql(query):
        return run(
            "exec",
            "-T",
            "db",
            "psql",
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "pullbox",
            stdin=query,
        )

    try:
        run("up", "-d", "--pull", "never", "--wait", "db")
        run(
            "exec",
            "-T",
            "db",
            "createuser",
            "-U",
            "postgres",
            "--no-superuser",
            "--no-createdb",
            "--no-createrole",
            "--pwprompt",
            "pullbox",
            stdin=password + "\n" + password + "\n",
        )
        run(
            "exec",
            "-T",
            "db",
            "createdb",
            "-U",
            "postgres",
            "--owner=pullbox",
            "--template=template0",
            "pullbox",
        )
        assert (
            sql(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole "
                "FROM pg_roles WHERE rolname='pullbox'"
            )
            == "f"
        )
        run("up", "-d", "--pull", "never", "--wait", "--wait-timeout", "110", "pullbox")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            account_password = secrets.token_hex(18) + "Aa1!"
            url = "http://" + run("port", "pullbox", "8585")
            setup = SetupWizardPage(page, url)
            setup.goto()
            setup.fill_username("freshoperator")
            setup.fill_password(account_password)
            setup.fill_confirm_password(account_password)
            setup.submit_account()
            page.wait_for_url("**/login")
            LoginPage(page, url).login("freshoperator", account_password)
            page.wait_for_url(url + "/")
            original_id = sql("SELECT id FROM users WHERE username='freshoperator'")
            assert original_id.isdigit()
            metric_id = sql(
                "SELECT id FROM dashboard_metric_rollups WHERE metric_key='active_downloads' "
                "ORDER BY bucket_start DESC LIMIT 1"
            )
            storage_id = sql(
                "SELECT id FROM dashboard_storage_snapshots WHERE total_bytes > 0 "
                "ORDER BY snapshot_date DESC LIMIT 1"
            )
            assert metric_id.isdigit() and storage_id.isdigit(), "Dashboard history was not saved"
            run("down")
            run("up", "-d", "--pull", "never", "--wait", "--wait-timeout", "110")
            url = "http://" + run("port", "pullbox", "8585")
            page = browser.new_page()
            LoginPage(page, url).login("freshoperator", account_password)
            page.wait_for_url(url + "/")
            page.goto(url + "/system?tab=about")
            expect(page.get_by_text(f"PostgreSQL {major}.", exact=False).first).to_be_visible()
            assert sql("SELECT id FROM users WHERE username='freshoperator'") == original_id
            assert sql(f"SELECT count(*) FROM dashboard_metric_rollups WHERE id={metric_id}") == "1"
            assert (
                sql(f"SELECT count(*) FROM dashboard_storage_snapshots WHERE id={storage_id}")
                == "1"
            )
            assert not run(
                "exec",
                "-T",
                "pullbox",
                "python",
                "-c",
                "from pathlib import Path; assert not Path('/data/pullbox.db').exists()",
            )
            browser.close()
    finally:
        # Only this randomly named test project owns these disposable volumes.
        run("down", "--volumes")
