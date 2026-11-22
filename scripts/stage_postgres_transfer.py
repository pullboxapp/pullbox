"""Prepare a dedicated offline PostgreSQL staging database without switching Pullbox."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import signal
import sqlite3
import sys
import warnings
from contextlib import AsyncExitStack, closing
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alembic.config import Config  # noqa: E402
from postgres_rehearsal_rows import STAGE_CLOSED_SQL, RehearsalError, compare_rows  # noqa: E402

from alembic import command  # noqa: E402
from pullbox.config import get_settings  # noqa: E402
from pullbox.services.database_migration_preflight import (  # noqa: E402
    PreflightReport,
    inspect_snapshot_connection,
)
from pullbox.services.migration_backup_input import (  # noqa: E402
    BackupInputError,
    database_snapshot,
)
from pullbox.services.migration_target_preflight import (  # noqa: E402
    inspect_target_connection,
    parse_target_url,
)


def _initialize(connection):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def _connect(resources, url, session_name):
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "timeout": 10,
            "command_timeout": 300,
            "server_settings": {
                "search_path": "pg_catalog",
                "timezone": "UTC",
                "lock_timeout": "5000",
                "application_name": session_name,
            },
        },
    )
    resources.push_async_callback(engine.dispose)
    return await resources.enter_async_context(engine.connect())


async def _unclaimed(conn, url, report):
    target = await inspect_target_connection(conn, url)
    if target.blockers:
        report["blockers"] = target.blockers
        return False
    if (
        await conn.scalar(
            text(
                "SELECT shobj_description(oid,'pg_database') "
                "FROM pg_database WHERE datname=current_database()"
            )
        )
        is not None
    ):
        raise RehearsalError("target_already_labelled")
    return True


async def stage(path, raw, report, *, review=False):
    url = parse_target_url(raw)
    if url is None:
        raise RehearsalError("target_url")
    configured = get_settings().db_url
    if make_url(configured).get_backend_name() != "sqlite":
        raise RehearsalError("sqlite_source_configuration_required")
    async with AsyncExitStack() as resources:
        with (
            database_snapshot(path, configured) as (snapshot, _),
            closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as db,
        ):
            db.execute("BEGIN")
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
            checks = PreflightReport()
            inspect_snapshot_connection(db, checks)
            if checks.blockers:
                report["blockers"] = checks.blockers
                return
            with snapshot.open("rb") as opened:
                source_hash = hashlib.file_digest(opened, "sha256").hexdigest()
            nonce = "pullbox_stage_" + uuid4().hex
            conn = await _connect(resources, url, nonce)
            if not await _unclaimed(conn, url, report):
                return
            name = conn.dialect.identifier_preparer.quote_identifier(url.database)
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            control = await _connect(
                resources, url.set(database="postgres"), "pullbox_stage_control"
            )
            if not await control.scalar(
                text(
                    "SELECT EXISTS(SELECT FROM pg_stat_activity WHERE pid=:pid AND datname=:db "
                    "AND usename=current_user AND application_name=:nonce) "
                    "AND current_database()='postgres'"
                ),
                {"pid": pid, "db": url.database, "nonce": nonce},
            ):
                raise RehearsalError("target_session_mismatch")
            await control.exec_driver_sql(f"ALTER DATABASE {name} ALLOW_CONNECTIONS false")
            # The ALTER holds the database object lock; check a fresh session/objects
            # snapshot before committing the reservation. On refusal the ALTER rolls back.
            await conn.commit()
            if not await _unclaimed(conn, url, report):
                return
            await control.exec_driver_sql(
                f"COMMENT ON DATABASE {name} IS 'pullbox:offline-stage:incomplete'"
            )
            await control.commit()
            report["target_closed"] = True
            # A client could have arrived between inspection and the committed fence.
            await conn.commit()
            target = await inspect_target_connection(conn, url)
            if target.blockers:
                report["blockers"] = target.blockers
                return
            if not await conn.scalar(text(STAGE_CLOSED_SQL)):
                raise RehearsalError("target_not_closed")
            await conn.exec_driver_sql("SET search_path TO public")
            await conn.commit()
            await conn.run_sync(_initialize)
            driver = (await conn.get_raw_connection()).driver_connection
            # SQLAlchemy deserializes JSON on this connection. For the exact-content
            # copy, use asyncpg's text-preserving codec, including JSON null vs SQL NULL.
            await driver.reset_type_codec("json", schema="pg_catalog")
            tables, sequences = await compare_rows(db, driver, checks.tables, retain=True)
        # The backup context has now verified the original inode, size, times and sidecars.
        await conn.exec_driver_sql("SET LOCAL search_path TO pg_catalog")
        if not await conn.scalar(text(STAGE_CLOSED_SQL)):
            raise RehearsalError("target_not_closed")
        await conn.exec_driver_sql("CREATE SCHEMA pullbox_stage")
        await conn.exec_driver_sql(
            "CREATE TABLE pullbox_stage.verification (snapshot_sha256 text PRIMARY KEY, "
            "source_revision text NOT NULL, "
            "checked_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        await conn.execute(
            text(
                "INSERT INTO pullbox_stage.verification (snapshot_sha256,source_revision) "
                "VALUES(:digest,:rev)"
            ),
            {"digest": source_hash, "rev": checks.source_revision},
        )
        await conn.exec_driver_sql(
            f"COMMENT ON DATABASE {name} IS 'pullbox:offline-stage:rows-verified'"
        )
        await conn.commit()
        report.update(
            rows_verified=True,
            snapshot_sha256=source_hash,
            verified_tables=tables,
            next_ids=sequences,
        )
        if review:
            from postgres_stage_review import review_library

            await review_library(conn, source_hash, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument(
        "--create-offline-stage",
        required=True,
        action="store_true",
        help="Close the explicitly selected empty target to new connections.",
    )
    parser.add_argument(
        "--url-stdin", action="store_true", help="Read target URL from protected stdin."
    )
    parser.add_argument(
        "--review-library",
        action="store_true",
        help="After copying, sign in to inspect the sealed library in a private terminal.",
    )
    args = parser.parse_args()
    report = {
        "rows_verified": False,
        "target_closed": False,
        "cutover_ready": False,
        "blockers": {},
    }
    try:
        if args.review_library and (
            args.url_stdin or not sys.stdin.isatty() or not sys.stderr.isatty()
        ):
            raise RehearsalError("review_requires_terminal")
        if args.url_stdin and sys.stdin.isatty():
            raise RehearsalError("use_hidden_prompt")
        if not args.url_stdin and not sys.stdin.isatty():
            raise RehearsalError("protected_target_input_required")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            raw = (
                sys.stdin.readline(4098).rstrip("\r\n")
                if args.url_stdin
                else getpass.getpass("Dedicated EMPTY PostgreSQL target URL: ")
            )
        asyncio.run(stage(args.backup, raw, report, review=args.review_library))
    except (RehearsalError, BackupInputError) as exc:
        report["blockers"] = {str(exc): "Staging stopped; do not activate the target."}
    except KeyboardInterrupt:
        report["blockers"] = {"cancelled": "Staging stopped; do not activate the target."}
    except Exception:
        # DB and driver exceptions may contain credentials or private data.
        report["blockers"] = {
            "stage_error": (
                "Staging could not finish. Inspect the stage status from the postgres database. "
                "Check owner connectivity, available storage and the selected backup. "
                "Use a fresh target if a reservation exists; do not reopen a partial copy."
            )
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["rows_verified"] and not report["blockers"] else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
