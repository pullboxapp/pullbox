"""Read-only inspection of an explicitly selected, uninitialized PostgreSQL database."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

_MESSAGES = {
    "target_url": "Provide an explicit PostgreSQL target URL with its owner, host and database. "
    "Remote connections require verified TLS. Do not use a maintenance database.",
    "driver": "Install Pullbox with the postgres extra or use an image that includes it.",
    "connection": "Could not inspect the target. Check reachability, TLS, and login permissions.",
    "target_version": "This empty-target check has only been qualified on PostgreSQL 16 and 18.",
    "target_identity": "The connected database is not the requested dedicated UTF-8 database.",
    "target_read_only": "Use a writable primary, not a standby or read-only database.",
    "target_ownership": "Use a non-administrative login that owns this database and can create "
    "objects in its public schema.",
    "target_shared": "Other roles can create objects or assume ownership. Use a dedicated target.",
    "target_not_empty": "The target already contains objects. Choose a new empty database; "
    "do not delete or truncate the existing one.",
    "target_busy": "Other sessions, prepared transactions or replication slots use this database. "
    "Keep the target offline and recheck.",
}


@dataclass
class TargetPreflight:
    server_major: int | None = None
    target_checks_passed: bool = False
    cutover_ready: bool = False
    blockers: dict[str, str] = field(default_factory=dict)
    next_step: str = "Inspection only. No schema or data was created. Do not switch databases."

    def block(self, key: str) -> None:
        self.blockers[key] = _MESSAGES[key]


def parse_target_url(raw: str) -> URL | None:
    """Accept only an explicitly named PostgreSQL target with a protected connection."""
    try:
        url = make_url(raw)
        if (
            url.drivername != "postgresql+asyncpg"
            or not url.username
            or not url.host
            or not url.database
            or url.database in {"template0", "template1", "postgres"}
            or set(url.query) - {"ssl"}
            or url.query.get("ssl") not in {None, "disable", "verify-full"}
            or (
                url.host not in {"127.0.0.1", "::1", "localhost"}
                and url.query.get("ssl") != "verify-full"
            )
            or any(ord(char) < 32 for char in raw)
            or len(raw) > 4096
        ):
            return None
    except (ArgumentError, ValueError):
        return None
    return url


# Only pg_catalog is in the lookup path, including during driver initialization.
# Do not execute user functions, inspect user rows, issue DDL, or acquire ownership.
_STATUS = text("""
    SELECT current_database() AS dbname,
           NOT d.datistemplate AND pg_encoding_to_char(d.encoding) = 'UTF8' AS dedicated,
           NOT pg_is_in_recovery() AND NOT current_setting('default_transaction_read_only')::bool
               AS writable,
           r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
               OR r.rolreplication OR r.rolbypassrls) AND r.oid = d.datdba
               AND ns.nspowner IN (r.oid, 'pg_database_owner'::regrole)
               AND has_schema_privilege(r.oid, ns.oid, 'USAGE')
               AND has_schema_privilege(r.oid, ns.oid, 'CREATE') AS owns_target,
           EXISTS (SELECT FROM aclexplode(ns.nspacl) a WHERE a.privilege_type = 'CREATE'
                     AND a.grantee NOT IN (r.oid, 'pg_database_owner'::regrole))
              OR EXISTS (SELECT FROM aclexplode(d.datacl) a WHERE a.privilege_type = 'CREATE'
                     AND a.grantee <> d.datdba)
              OR EXISTS (SELECT FROM pg_auth_members WHERE roleid = r.oid) AS shared,
           EXISTS (SELECT FROM pg_stat_activity WHERE datid=d.oid AND pid <> pg_backend_pid())
              OR EXISTS (SELECT FROM pg_prepared_xacts WHERE database=current_database())
              OR EXISTS (SELECT FROM pg_replication_slots WHERE datoid=d.oid) AS busy
      FROM pg_database d
      JOIN pg_roles r ON r.rolname=current_user
      LEFT JOIN pg_namespace ns ON ns.nspname='public'
     WHERE d.datname=current_database()
""")

# 16384 is FirstNormalObjectId on both qualified majors. Check low-numbered
# large objects separately: PostgreSQL lets their creator choose the OID.
_OCCUPIED = text("""
    SELECT EXISTS (SELECT FROM pg_namespace
                    WHERE nspname NOT IN ('pg_catalog','pg_toast','information_schema','public'))
        OR EXISTS (SELECT FROM pg_class WHERE oid >= 16384 OR relnamespace='public'::regnamespace)
        OR EXISTS (SELECT FROM pg_type WHERE oid >= 16384 OR typnamespace='public'::regnamespace)
        OR EXISTS (SELECT FROM pg_proc WHERE oid >= 16384 OR pronamespace='public'::regnamespace)
        OR EXISTS (SELECT FROM pg_extension WHERE extname <> 'plpgsql')
        OR EXISTS (SELECT FROM pg_depend WHERE objid >= 16384)
        OR EXISTS (SELECT FROM pg_largeobject_metadata)
        OR EXISTS (SELECT FROM pg_foreign_data_wrapper)
        OR EXISTS (SELECT FROM pg_publication)
        OR EXISTS (SELECT FROM pg_subscription WHERE subdbid =
                    (SELECT oid FROM pg_database WHERE datname=current_database()))
        OR EXISTS (SELECT FROM pg_event_trigger)
        OR EXISTS (SELECT FROM pg_default_acl)
""")


async def inspect_target_connection(conn: AsyncConnection, url: URL) -> TargetPreflight:
    """Check eligibility on the caller's connection with pg_catalog as its lookup path."""
    report = TargetPreflight()
    major = await conn.scalar(text("SELECT current_setting('server_version_num')::int / 10000"))
    report.server_major = int(major)
    if report.server_major not in {16, 18}:
        report.block("target_version")
        return report
    row = (await conn.execute(_STATUS)).mappings().one()
    if not row["dedicated"] or row["dbname"] != url.database:
        report.block("target_identity")
    if not row["writable"]:
        report.block("target_read_only")
    if not row["owns_target"]:
        report.block("target_ownership")
    if row["shared"]:
        report.block("target_shared")
    if row["busy"]:
        report.block("target_busy")
    if not report.blockers and await conn.scalar(_OCCUPIED):
        report.block("target_not_empty")
    report.target_checks_passed = not report.blockers
    return report


async def inspect_target(raw: str) -> TargetPreflight:
    report = TargetPreflight()
    url = parse_target_url(raw)
    if url is None:
        report.block("target_url")
        return report
    try:
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "timeout": 5,
                "command_timeout": 5,
                "server_settings": {
                    "search_path": "pg_catalog",
                    "statement_timeout": "5000",
                    "application_name": "pullbox_migration_check",
                },
            },
        )
        try:
            async with asyncio.timeout(12), engine.begin() as conn:
                await conn.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                report = await inspect_target_connection(conn, url)
        finally:
            await engine.dispose()
    except ModuleNotFoundError:
        report.block("driver")
    except Exception:
        # Authentication during driver startup can escape SQLAlchemy's wrappers.
        # Keep native diagnostics and connection parameters out of the user report.
        report.block("connection")
    report.target_checks_passed = not report.blockers
    return report
