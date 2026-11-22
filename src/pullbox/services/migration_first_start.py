"""Bind an operator-controlled first-start secret to a reviewed, still-closed stage."""

import asyncio
import hmac
import json
import os
import re
import stat
from pathlib import Path

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from pullbox.services.migration_target_preflight import parse_target_url

VERIFIED = "pullbox:offline-stage:rows-verified"
PREFIX = "pullbox:offline-stage:bound-v1:"
SOURCE_PREFIX = "pullbox:offline-stage:bound-v2:"
_HEX = re.compile(r"[a-f0-9]{64}")


class BindingError(ValueError):
    """A safe, operator-facing reason the handoff cannot be verified."""


def require_startable_target(conn: Connection, *, stage_initializer: bool = False) -> None:
    """Reject migration-owned targets before normal migrations, recovery, or scheduling."""
    if conn.dialect.name != "postgresql":
        return
    label, closed, bound = conn.exec_driver_sql(
        "SELECT pg_catalog.shobj_description(oid,'pg_database'), NOT datallowconn, "
        "pg_catalog.to_regclass('pullbox_stage.first_start_binding') IS NOT NULL "
        "FROM pg_catalog.pg_database WHERE datname=current_database()"
    ).one()
    if stage_initializer and closed and label == "pullbox:offline-stage:incomplete" and not bound:
        return
    if stage_initializer or bound or (label or "").startswith("pullbox:offline-stage:"):
        raise RuntimeError(
            "Offline migration stage: normal startup is not authorized. Keep the SQLite "
            "installation and backups intact; do not remove the stage marker."
        )


def read_token(path: Path) -> bytes:
    """Accept only a bounded, private regular file, never an env or CLI secret."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077:
                value = os.read(fd, 66)
                if re.fullmatch(rb"[0-9a-f]{64}\n?", value):
                    return bytes.fromhex(value.decode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        pass
    raise BindingError("Mount a private, readable token file containing 64 lowercase hex digits.")


async def _identity(conn: AsyncConnection, database: str, *, enrolling: bool) -> tuple[str, str]:
    # The server start time deliberately invalidates a pending handoff on restart,
    # without requiring privileged access to PostgreSQL's cluster control functions.
    row = (
        await conn.execute(
            text("""
        SELECT d.oid, current_user, extract(epoch from pg_postmaster_start_time())::text,
               shobj_description(d.oid, 'pg_database')
        FROM pg_database d WHERE d.datname=:db AND NOT d.datallowconn
          AND NOT d.datistemplate AND NOT pg_is_in_recovery()
          AND current_setting('server_version_num')::int / 10000 IN (16,18)
          AND d.datdba=current_user::regrole
          AND current_database() = CASE WHEN :enrolling THEN d.datname ELSE 'postgres' END
          AND NOT EXISTS (SELECT FROM pg_stat_activity
                          WHERE datid=d.oid AND pid<>pg_backend_pid())
    """),
            {"db": database, "enrolling": enrolling},
        )
    ).one_or_none()
    if not row:
        raise BindingError("The expected offline target was not found, is open, or is in use.")
    return json.dumps([database, *row[:3]], separators=(",", ":")), row[3] or ""


def _label(key: bytes, identity: str, digest: str, source: str | None) -> str:
    prefix = PREFIX if source is None else SOURCE_PREFIX
    fields = digest if source is None else digest + ":" + source
    proof = hmac.digest(key, (prefix + identity + ":" + fields).encode(), "sha256").hex()
    return prefix + fields + ":" + proof


async def bind_reviewed_stage(
    conn: AsyncConnection, database: str, digest: str, key: bytes, *, source: str | None = None
) -> None:
    """Called only after a confirmed review, on its original staging connection."""
    identity, current = await _identity(conn, database, enrolling=True)
    if (
        current != VERIFIED
        or await conn.scalar(text("SELECT snapshot_sha256 FROM pullbox_stage.verification"))
        != digest
        or not _HEX.fullmatch(digest)
        or (source is not None and not _HEX.fullmatch(source))
    ):
        raise BindingError("The target no longer matches the reviewed stage.")
    name = conn.dialect.identifier_preparer.quote_identifier(database)
    label = _label(key, identity, digest, source)
    # A database clone or dump can lose the shared catalog comment. Keep the fence
    # with the library too, atomically with the public, non-secret attestation.
    await conn.exec_driver_sql(
        "CREATE TABLE pullbox_stage.first_start_binding (snapshot_sha256 text PRIMARY KEY "
        "REFERENCES pullbox_stage.verification(snapshot_sha256), label text NOT NULL)"
    )
    await conn.execute(
        text("INSERT INTO pullbox_stage.first_start_binding VALUES (:digest,:label)"),
        {"digest": digest, "label": label},
    )
    # COMMENT does not accept a bind parameter. The label contains only fixed text and hex.
    await conn.exec_driver_sql(f"COMMENT ON DATABASE {name} IS '{label}'")
    if await _identity(conn, database, enrolling=True) != (identity, label):
        raise BindingError("The target changed during binding. Keep it offline.")
    await conn.commit()


async def check_binding(
    raw: str, token_file: Path, digest: str, *, source: str | None = None
) -> None:
    """Use the deployment's configured owner via postgres; never connect to the stage."""
    url = parse_target_url(raw)
    if url is None or not url.database or not _HEX.fullmatch(digest):
        raise BindingError("Check the configured PostgreSQL target and staged snapshot SHA-256.")
    key = read_token(token_file)
    try:
        engine = create_async_engine(
            url.set(database="postgres"),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "timeout": 8,
                "command_timeout": 8,
                "server_settings": {
                    "search_path": "pg_catalog",
                    "application_name": "pullbox_binding_check",
                    "default_transaction_read_only": "on",
                },
            },
        )
        try:
            async with asyncio.timeout(12), engine.connect() as conn:
                identity, current = await _identity(conn, url.database, enrolling=False)
                if source is not None and current.startswith(PREFIX):
                    raise BindingError(
                        "This stage has no source binding. Keep it closed; review a new stage "
                        "with the original --source-capture-report."
                    )
                # The deployment-only probe can read either format. A host handoff
                # must match the separately supplied source, never one from this label.
                if source is None and current.startswith(SOURCE_PREFIX):
                    fields = current.removeprefix(SOURCE_PREFIX).split(":")
                    if len(fields) != 3 or not _HEX.fullmatch(fields[1]):
                        raise BindingError("The source binding is not valid.")
                    source = fields[1]
                if not hmac.compare_digest(
                    current.encode(), _label(key, identity, digest, source).encode()
                ):
                    raise BindingError(
                        "The token, capture, snapshot or PostgreSQL instance does not match "
                        "the review."
                    )
        finally:
            await engine.dispose()
    except (OSError, SQLAlchemyError, ModuleNotFoundError) as exc:
        raise BindingError(
            "Could not check the target. Verify the owner, connection, and TLS."
        ) from exc
