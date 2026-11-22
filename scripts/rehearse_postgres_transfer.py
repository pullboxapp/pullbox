"""Rehearse a SQLite transfer in a disposable PostgreSQL container, never in a live target."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from functools import partial
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from postgres_rehearsal_rows import RehearsalError, compare_rows  # noqa: E402

from pullbox.config import get_settings  # noqa: E402
from pullbox.services.database_migration_preflight import (  # noqa: E402
    PreflightReport,
    inspect_snapshot_connection,
)
from pullbox.services.migration_backup_input import (  # noqa: E402
    BackupInputError,
    database_snapshot,
)


def _docker(*args, endpoint=None, **kwargs):
    host = ["--host", endpoint] if endpoint else []
    return subprocess.run(
        ["docker", *host, *args], capture_output=True, text=True, timeout=60, **kwargs
    )


@contextmanager
def _container(major):
    endpoint = (
        os.environ.get("DOCKER_HOST")
        or _docker(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", check=True
        ).stdout.strip()
    )
    # No remote daemon, inherited remote context override, persistent data mount or external target.
    if not endpoint.startswith("unix://") or (
        os.environ.get("DOCKER_CONTEXT") and os.environ.get("DOCKER_HOST")
    ):
        raise RehearsalError("local_docker_required")
    docker = partial(_docker, endpoint=endpoint)
    image = f"postgres:{major}-bookworm"
    if docker("image", "inspect", "--format", "{{.Id}}", image).returncode:
        raise RehearsalError("local_postgres_image_required")
    name = "pullbox-transfer-trial-" + uuid4().hex
    mount = "/var/lib/postgresql/data" if major == "16" else "/var/lib/postgresql"
    password = secrets.token_hex(32)
    cid = docker(
        "create",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--log-driver",
        "none",
        "--memory=4g",
        "--memory-swap=4g",
        "--label",
        "pullbox.offline-transfer=disposable",
        "--tmpfs",
        mount + ":size=3g",
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_PASSWORD",
        image,
        env={**os.environ, "POSTGRES_PASSWORD": secrets.token_hex(32)},
        check=True,
    ).stdout.strip()
    try:
        docker("start", cid, check=True)
        for _ in range(120):
            if not docker(
                "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"
            ).returncode:
                break
            time.sleep(0.25)
        else:
            raise RehearsalError("disposable_database_not_ready")
        docker(
            "exec",
            "-i",
            cid,
            "psql",
            "-Xq",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            input=f"CREATE ROLE trial_owner LOGIN PASSWORD '{password}';\n"
            "CREATE DATABASE pullbox_trial OWNER trial_owner TEMPLATE template0;\n",
            check=True,
        )
        port = int(docker("port", cid, "5432/tcp", check=True).stdout.strip().rsplit(":", 1)[1])
        yield f"postgresql://trial_owner:{password}@127.0.0.1:{port}/pullbox_trial"
    finally:
        if docker("rm", "--force", "--volumes", cid).returncode:
            remaining = docker("ps", "-aq", "--no-trunc", "--filter", "id=" + cid)
            if remaining.returncode or remaining.stdout.strip():
                raise RehearsalError("check_disposable_container_cleanup")


async def _copy(db, dsn, counts, major):
    import asyncpg

    pg = await asyncpg.connect(dsn, timeout=10, server_settings={"timezone": "UTC"})
    try:
        if pg.get_server_version().major != int(major):
            raise RehearsalError("unexpected_server_major")
        return await compare_rows(db, pg, counts)
    finally:
        await pg.close(timeout=10)


def _run(path, major, report):
    with (
        database_snapshot(path, get_settings().db_url) as (snapshot, _),
        closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as db,
    ):
        db.execute("BEGIN")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
        checks = PreflightReport()
        inspect_snapshot_connection(db, checks)
        report["source_revision"] = checks.source_revision
        if checks.blockers:
            report["blockers"] = checks.blockers
            return
        with _container(major) as dsn:
            report["target_started"] = True
            url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
            migrated = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "alembic/alembic.ini",
                    "upgrade",
                    "head",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT / "src"),
                    "PULLBOX_DB_URL": url,
                    "PULLBOX_SECRET_KEY": secrets.token_hex(32),
                },
                capture_output=True,
                timeout=180,
            )
            if migrated.returncode:
                raise RehearsalError("disposable_schema_setup")
            tables, sequences = asyncio.run(_copy(db, dsn, checks.tables, major))
    # Set success only after the snapshot's final stability check and container removal.
    report.update(rehearsal_passed=True, verified_tables=tables, next_ids=sequences)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--postgres-major", choices=("16", "18"), required=True)
    args = parser.parse_args()
    report = {
        "rehearsal_passed": False,
        "cutover_ready": False,
        "target_started": False,
        "postgres_major": int(args.postgres_major),
        "blockers": {},
    }
    try:
        _run(args.backup, args.postgres_major, report)
    except (RehearsalError, BackupInputError) as exc:
        report["blockers"] = {str(exc): "Rehearsal stopped. No cutover was performed."}
    except KeyboardInterrupt:
        report["blockers"] = {"cancelled": "Rehearsal cancelled."}
    except Exception as exc:
        # Driver, subprocess and decoding exceptions can include private row values or credentials.
        report["blockers"] = {"rehearsal_error": type(exc).__name__}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["rehearsal_passed"] else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
