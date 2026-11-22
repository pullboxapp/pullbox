"""The target probe must never create, clean, or connect the application to a database."""

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest


def check(url):
    run = subprocess.run(
        [sys.executable, "-m", "pullbox.cli", "migration-check-target", "--url-stdin", "--json"],
        input=url + "\n",
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "PULLBOX_DB_URL": "sqlite:////do-not-use/live.db"},
    )
    assert run.returncode in {0, 1}, "The CLI does not provide a target check"
    assert "postgresql+" not in run.stdout + run.stderr and not run.stderr
    return run.returncode, json.loads(run.stdout)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sqlite:////data/pullbox.db",
        "postgresql+asyncpg://localhost/postgres",
        "postgresql+asyncpg://user@host/template1",
        "postgresql+asyncpg://user@remote.example/db?ssl=disable",
        "postgresql+asyncpg://user@localhost/db?host=elsewhere",
        "postgresql+asyncpg://user@localhost/",
        "postgresql+asyncpg:///db",
    ],
)
def test_declines_implicit_unsafe_or_non_target_urls(url):
    code, report = check(url)
    assert code == 1 and "target_url" in report["blockers"]
    assert not report["target_checks_passed"] and not report["cutover_ready"]


def test_unreachable_target_is_bounded_and_redacted():
    code, report = check("postgresql+asyncpg://private:DO_NOT_DISCLOSE@127.0.0.1:1/private_db")
    assert code == 1 and "connection" in report["blockers"]
    assert "DO_NOT_DISCLOSE" not in str(report) and "private" not in str(report)


@pytest.fixture(scope="module", params=[16, 18])
def server(request):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("opt in to disposable Docker PostgreSQL 16 and 18")
    major = request.param
    password = secrets.token_hex(20)
    container = "pullbox-target-probe-" + uuid4().hex

    def docker(*args, **kwargs):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=45, **kwargs
        )

    mount = "/var/lib/postgresql/data" if major == 16 else "/var/lib/postgresql"
    docker(
        "run",
        "--rm",
        "--detach",
        "--pull=never",
        "--name",
        container,
        "--tmpfs",
        mount,
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_PASSWORD",
        f"postgres:{major}-bookworm",
        env={**os.environ, "POSTGRES_PASSWORD": password},
        check=True,
    )
    try:
        for _ in range(100):
            if not docker(
                "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"
            ).returncode:
                break
            time.sleep(0.15)
        else:
            pytest.fail("Disposable database did not become ready")

        def sql(database, command):
            return docker(
                "exec",
                "-i",
                container,
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                database,
                input=command,
                check=True,
            ).stdout.strip()

        sql("postgres", f"CREATE ROLE probe_owner LOGIN PASSWORD '{password}';")
        port = int(docker("port", container, "5432/tcp", check=True).stdout.strip().split(":")[-1])
        yield major, password, port, sql
    finally:
        docker("rm", "-f", container, check=True)


@pytest.mark.parametrize(
    ("setup", "blocker"),
    [
        ("", None),
        (
            "CREATE TABLE public.other_library(id int); "
            "INSERT INTO public.other_library VALUES (42)",
            "target_not_empty",
        ),
        ("CREATE SCHEMA elsewhere", "target_not_empty"),
        ("CREATE TYPE public.marker AS ENUM ('x')", "target_not_empty"),
        ("SELECT lo_create(1234)", "target_not_empty"),
        ("GRANT CREATE ON SCHEMA public TO PUBLIC", "target_shared"),
        ("ALTER SCHEMA public OWNER TO postgres", "target_ownership"),
        ("ALTER DATABASE {db} OWNER TO postgres", "target_ownership"),
        ("ALTER DATABASE {db} SET default_transaction_read_only = on", "target_read_only"),
    ],
)
def test_dedicated_target(server, setup, blocker):
    major, password, port, sql = server
    database = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {database} OWNER probe_owner TEMPLATE template0")
    if setup:
        sql(database, setup.format(db=database))
    code, report = check(f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{database}")
    assert report["server_major"] == major and not report["cutover_ready"]
    assert password not in str(report) and database not in str(report)
    assert code == int(blocker is not None)
    assert report["target_checks_passed"] == (blocker is None)
    if blocker:
        assert blocker in report["blockers"]
    if "other_library" in setup:
        assert sql(database, "SELECT id FROM public.other_library") == "42"
    else:
        assert (
            sql(database, "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace")
            == "0"
        )


def test_bad_password_is_reported_without_a_driver_trace(server):
    _, _, port, sql = server
    db = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    code, report = check(f"postgresql+asyncpg://probe_owner:DO_NOT_DISCLOSE@127.0.0.1:{port}/{db}")
    assert code == 1 and "connection" in report["blockers"]
    assert "DO_NOT_DISCLOSE" not in str(report)


async def test_connected_target_is_blocked_then_passes_after_disconnect(server):
    from sqlalchemy.ext.asyncio import create_async_engine

    _, password, port, sql = server
    db = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    url = f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{db}"
    # Use a different login so the probe has no special access to that session.
    engine = create_async_engine(f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/{db}")
    try:
        async with engine.connect():
            code, report = check(url)
            assert code == 1 and "target_busy" in report["blockers"]
    finally:
        await engine.dispose()
    assert check(url)[0] == 0


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.dev"])
def test_images_install_the_existing_postgres_extra(name):
    recipe = (Path(__file__).resolve().parents[2] / "docker" / name).read_text()
    group = "prod" if name == "Dockerfile" else "dev"
    assert f'".[{group},postgres]"' in recipe
