"""Operator-controlled activation of a source-bound, reviewed PostgreSQL stage."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import AsyncExitStack, closing
from typing import TYPE_CHECKING, Any

from sqlalchemy import Integer, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from pullbox.models import Base
from pullbox.services.database_migration_preflight import SOURCE_REVISION
from pullbox.services.migration_backup_input import database_snapshot
from pullbox.services.migration_first_start import (
    SOURCE_PREFIX,
    BindingError,
    _identity,
    _label,
    read_token,
)
from pullbox.services.migration_row_comparison import (
    RehearsalError,
    _q,
    _verify_table,
    table_snapshot,
)
from pullbox.services.migration_target_preflight import inspect_target_connection, parse_target_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import AsyncConnection


def sign_grant(key: bytes, binding: str, challenge: str, *, issued: int | None = None) -> str:
    stamp = str(int(time.time()) if issued is None else issued)
    material = f"pullbox:admit:v1:{binding}:{challenge}:{stamp}".encode()
    return stamp + "." + hmac.digest(key, material, "sha256").hex()


async def send_grant(raw: str, binding: str, challenge: str, token: Path) -> None:
    url = parse_target_url(raw)
    if not url or not url.database or not re.fullmatch("[a-f0-9]{32}", challenge):
        raise BindingError("Invalid waiting target or challenge.")
    channel = "pullbox_admit_" + challenge
    async with AsyncExitStack() as stack:
        ctl = await _connect(stack, url.set(database="postgres"), "pullbox_host_confirmation")
        _, current = await _identity(ctl, url.database, enrolling=False, waiting=channel)
        if binding != current:
            raise BindingError("The waiting target changed before confirmation.")
        await ctl.execute(
            text("SELECT pg_notify(:channel,:proof)"),
            {"channel": channel, "proof": sign_grant(read_token(token), binding, challenge)},
        )
        await ctl.commit()


async def _connect(stack: AsyncExitStack, url: URL, name: str) -> AsyncConnection:
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "timeout": 10,
            "command_timeout": 300,
            "server_settings": {
                "application_name": name,
                "search_path": "pg_catalog",
                "timezone": "UTC",
                "lock_timeout": "5000",
            },
        },
    )
    stack.push_async_callback(engine.dispose)
    return await stack.enter_async_context(engine.connect())


async def _check_foreign_keys(db: sqlite3.Connection, pg: Any, tables: set[str]) -> None:
    # Match relationships by their columns and actions, not backend-generated constraint names.
    actions = {
        "NO ACTION": "a",
        "RESTRICT": "r",
        "CASCADE": "c",
        "SET NULL": "n",
        "SET DEFAULT": "d",
    }
    target: dict[str, list[tuple[object, ...]]] = {}
    for row in await pg.fetch("""
        SELECT src.relname,dst.relname,
          ARRAY(SELECT attname::text FROM unnest(f.conkey) WITH ORDINALITY k(num,pos)
                JOIN pg_attribute ON attrelid=src.oid AND attnum=k.num ORDER BY k.pos),
          ARRAY(SELECT attname::text FROM unnest(f.confkey) WITH ORDINALITY k(num,pos)
                JOIN pg_attribute ON attrelid=dst.oid AND attnum=k.num ORDER BY k.pos),
          f.confupdtype::text,f.confdeltype::text,
          f.convalidated AND dst.relnamespace='public'::regnamespace
        FROM pg_constraint f JOIN pg_class src ON f.conrelid=src.oid
        JOIN pg_class dst ON f.confrelid=dst.oid
        WHERE f.contype='f' AND src.relnamespace='public'::regnamespace
    """):
        name, dest, left, right, update, delete, valid = row
        if not valid:
            raise BindingError("foreign_keys:" + name)
        target.setdefault(name, []).append((dest, tuple(left), tuple(right), update, delete))
    for name in tables:
        groups: dict[int, list[Any]] = {}
        for row in db.execute("SELECT * FROM pragma_foreign_key_list(?) ORDER BY id,seq", (name,)):
            groups.setdefault(row[0], []).append(row)
        expected = [
            (
                rows[0][2],
                tuple(r[3] for r in rows),
                tuple(r[4] for r in rows),
                actions[rows[0][5]],
                actions[rows[0][6]],
            )
            for rows in groups.values()
        ]
        if sorted(expected) != sorted(target.get(name, [])):
            raise BindingError("foreign_keys:" + name)


async def _reconcile(raw: str, backup: Path, digest: str, pg: Any) -> None:
    with database_snapshot(backup, raw) as (path, _), path.open("rb") as blob:
        if hashlib.file_digest(blob, "sha256").hexdigest() != digest:
            raise BindingError("The restored snapshot changed before admission.")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
            db.execute("BEGIN")
            if await pg.fetch("SELECT version_num FROM public.alembic_version") != [
                (SOURCE_REVISION,)
            ]:
                raise BindingError("The staged schema revision changed.")
            layout: dict[str, dict[str, str]] = {}
            for name, column, kind in await pg.fetch(
                "SELECT table_name,column_name,data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name<>'alembic_version'"
            ):
                layout.setdefault(name, {})[column] = kind
            if set(layout) != set(Base.metadata.tables):
                raise BindingError("The staged table layout changed.")
            await _check_foreign_keys(db, pg, set(layout))
            for name, columns in layout.items():
                table = "public." + _q(name)
                source = await table_snapshot(db, pg, name, columns, None, copy=False)
                await _verify_table(pg, name, source)
                if "id" not in columns:
                    continue
                seq = await pg.fetchrow(
                    "SELECT n.nspname,c.relname,s.seqincrement,s.seqcycle,s.seqcache,s.seqmax, "
                    "a.attidentity IN ('a','d') OR pg_get_expr(ad.adbin,ad.adrelid)="
                    "format('nextval(%L::regclass)',c.oid::regclass::text) "
                    "FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid=$1::text::regclass AND a.attname='id' "
                    "LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum "
                    "WHERE c.oid=pg_get_serial_sequence($1,'id')::regclass",
                    table,
                )
                if seq is None:
                    col = Base.metadata.tables[name].autoincrement_column
                    if col is not None and col.name == "id" and isinstance(col.type, Integer):
                        raise BindingError("sequence_missing:" + name)
                    continue
                previous = db.execute(
                    "SELECT max(seq) FROM sqlite_sequence WHERE name=?", (name,)
                ).fetchone()[0]
                high = max(source.high_id, previous or 0)
                if (
                    seq[0] != "public"
                    or tuple(seq[2:5]) != (1, False, 1)
                    or high >= seq[5]
                    or not seq[6]
                ):
                    raise BindingError("sequence_mismatch:" + name)
                # nextval/setval are not transactional. Never consume or repair an ID here.
                value, called = await pg.fetchrow(
                    f"SELECT last_value,is_called FROM public.{_q(seq[1])}"
                )
                if (value, called) != (max(1, high), high > 0):
                    raise BindingError("sequence_mismatch:" + name)


async def admit_verified_stage(
    raw: str,
    backup: Path,
    token: Path,
    digest: str,
    binding: str,
    expires: int,
    get_grant: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """Consume the live host decision in the verified runtime, never from a saved report."""
    url = parse_target_url(raw)
    if (
        not url
        or not url.database
        or not binding.startswith(SOURCE_PREFIX)
        or time.time() >= expires
    ):
        raise BindingError("A current source-bound deployment approval is required.")
    database = url.database
    name = _q(database)
    key = read_token(token)
    challenge = secrets.token_hex(16)
    async with AsyncExitStack() as stack:
        ctl = await _connect(stack, url.set(database="postgres"), "pullbox_cutover_control")
        if not await ctl.scalar(
            text("SELECT pg_try_advisory_lock(105145,hashtext(:db))"), {"db": database}
        ):
            raise BindingError("Another first start is in progress.")
        identity, current = await _identity(ctl, database, enrolling=False)
        source = binding.removeprefix(SOURCE_PREFIX).split(":")
        if (
            len(source) != 3
            or not re.fullmatch(r"[a-f0-9]{64}", source[1])
            or current != binding
            or not hmac.compare_digest(_label(key, identity, digest, source[1]), binding)
        ):
            raise BindingError("The reviewed stage or its binding changed.")
        # Admit one inspection session. The app and Alembic still refuse both stage markers.
        # Take the database object lock and recheck its identity before changing admission.
        await ctl.exec_driver_sql(f"ALTER DATABASE {name} ALLOW_CONNECTIONS false")
        await ctl.exec_driver_sql("SELECT pg_stat_clear_snapshot()")
        if await _identity(ctl, database, enrolling=False) != (identity, current):
            raise BindingError("The target changed before inspection.")
        await ctl.exec_driver_sql(f"ALTER DATABASE {name} ALLOW_CONNECTIONS true")
        await ctl.commit()
        try:
            conn = await _connect(stack, url, "pullbox_stage_inspection")
        finally:
            await ctl.exec_driver_sql(f"ALTER DATABASE {name} ALLOW_CONNECTIONS false")
            await ctl.commit()
        if await _identity(conn, database, enrolling=True) != (identity, current):
            raise BindingError("The original stage is not exclusively held. Leave it offline.")
        target = await inspect_target_connection(conn, url)
        if set(target.blockers) != {"target_not_empty"}:
            raise BindingError("The stage no longer has the required ownership or isolation.")
        marker = (
            await conn.exec_driver_sql(
                "SELECT b.snapshot_sha256,b.label,v.source_revision "
                "FROM pullbox_stage.first_start_binding b "
                "JOIN pullbox_stage.verification v USING(snapshot_sha256)"
            )
        ).all()
        if [tuple(row) for row in marker] != [(digest, binding, SOURCE_REVISION)]:
            raise BindingError("The stage's internal verification record does not match.")
        driver = (await conn.get_raw_connection()).driver_connection
        if driver is None:
            raise BindingError("Lost the verification connection. Leave the stage offline.")
        await driver.reset_type_codec("json", schema="pg_catalog")
        try:
            await _reconcile(raw, backup, digest, driver)
        except RehearsalError as exc:
            raise BindingError(str(exc)) from exc
        channel = "pullbox_admit_" + challenge
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        if get_grant is None:
            observer = (await ctl.get_raw_connection()).driver_connection
            if observer is None:
                raise BindingError("Lost the first-start confirmation connection.")

            def receive(_: Any, pid: int, name: str, proof: str) -> None:
                if not result.done() and name == channel:
                    result.set_result(proof)

            await observer.add_listener(channel, receive)
            await ctl.commit()
        await conn.execute(
            text("SELECT set_config('application_name',:name,false)"),
            {"name": channel},
        )
        await conn.commit()
        if get_grant is None:
            print(f"First-start challenge: {challenge}", flush=True)
            print(
                "Run check_postgres_handoff.py --authorize-start with this challenge "
                "on the SQLite host. Waiting; workers are off.",
                flush=True,
            )
            try:
                grant = await asyncio.wait_for(result, max(0, expires - time.time()))
            except TimeoutError as exc:
                raise BindingError(
                    "Approval expired while waiting. The stage remains closed."
                ) from exc
        else:
            grant = await get_grant(challenge)
        stamp, _, _ = grant.partition(".")
        now = time.time()
        if (
            not re.fullmatch(r"[0-9]{1,12}\.[a-f0-9]{64}", grant)
            or not 0 <= now - int(stamp) < 60
            or now >= expires
            or not hmac.compare_digest(
                grant, sign_grant(key, binding, challenge, issued=int(stamp))
            )
        ):
            raise BindingError(
                "The live host confirmation did not match or expired. The stage remains closed."
            )
        if await _identity(conn, database, enrolling=True) != (identity, current):
            raise BindingError("Target changed before activation.")
        # All three changes commit together. A disconnect before commit is not admission.
        await conn.exec_driver_sql(
            "ALTER TABLE pullbox_stage.first_start_binding RENAME TO completed_first_start"
        )
        await conn.exec_driver_sql(f"COMMENT ON DATABASE {name} IS NULL")
        await conn.exec_driver_sql(f"ALTER DATABASE {name} ALLOW_CONNECTIONS true")
        await conn.commit()
