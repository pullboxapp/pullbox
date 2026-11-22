"""Copy and compare application rows inside a newly generated throwaway database."""

from __future__ import annotations

from pullbox.models import Base
from pullbox.services.database_migration_preflight import SOURCE_REVISION
from pullbox.services.migration_row_comparison import (
    RehearsalError,
    _q,
    _verify_table,
    table_snapshot,
)

STAGE_CLOSED_SQL = (
    "SELECT NOT datallowconn AND pg_catalog.shobj_description(oid,'pg_database')="
    "'pullbox:offline-stage:incomplete' AND NOT EXISTS(SELECT FROM pg_catalog.pg_stat_activity "
    "WHERE datid=d.oid AND pid<>pg_catalog.pg_backend_pid()) "
    "FROM pg_catalog.pg_database d WHERE datname=pg_catalog.current_database()"
)


async def compare_rows(db, pg, counts, *, retain=False):
    """Caller owns the newly initialized isolated target; disposable trials always roll back."""
    import asyncpg

    if retain and not await pg.fetchval(STAGE_CLOSED_SQL):
        raise RehearsalError("target_not_closed")
    if await pg.fetchval("SELECT version_num FROM public.alembic_version") != SOURCE_REVISION:
        raise RehearsalError("target_revision")
    definitions = {}
    for row in await pg.fetch(
        "SELECT table_name,column_name,data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name<>'alembic_version'"
    ):
        definitions.setdefault(row[0], {})[row[1]] = row[2]
    if set(definitions) != set(counts) or set(counts) != set(Base.metadata.tables):
        raise RehearsalError("table_layout")
    snapshots, verified, next_ids = {}, {}, {}
    tx = pg.transaction()
    await tx.start()
    try:
        # Only migration-generated seed rows in this new container may be replaced.
        await pg.execute("TRUNCATE " + ",".join("public." + _q(t) for t in sorted(counts)))
        fks = await pg.fetch(
            "SELECT c.relname,k.conname FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid WHERE k.contype='f' "
            "AND k.connamespace='public'::regnamespace AND NOT k.condeferrable"
        )
        for table, constraint in fks:
            await pg.execute(
                f"ALTER TABLE public.{_q(table)} ALTER CONSTRAINT {_q(constraint)} DEFERRABLE"
            )
        await pg.execute("SET CONSTRAINTS ALL DEFERRED")
        for table in sorted(counts):
            try:
                snapshots[table] = await table_snapshot(
                    db, pg, table, definitions[table], counts[table], copy=True
                )
            except asyncpg.PostgresError as exc:
                field = exc.column_name
                if field not in definitions[table]:
                    field = next(
                        (c for c in definitions[table] if f", column {c}:" in (exc.context or "")),
                        None,
                    )
                category = {
                    "22001": "text_length",
                    "22003": "numeric_range",
                    "22P02": "invalid_representation",
                    "23502": "required_value",
                    "23505": "uniqueness",
                    "23514": "check_constraint",
                    "22021": "text_encoding",
                }.get(exc.sqlstate, "database_value")
                known = table + ("." + field if field in definitions[table] else "")
                raise RehearsalError(category + ":" + known) from None
            except (OverflowError, UnicodeError, ValueError):
                raise RehearsalError("target_value:" + table) from None
        await pg.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for table, constraint in fks:
            await pg.execute(
                f"ALTER TABLE public.{_q(table)} ALTER CONSTRAINT {_q(constraint)} NOT DEFERRABLE"
            )
        # Compare the final state, after every insert and deferred relationship check.
        for table, snapshot in snapshots.items():
            verified[table] = await _verify_table(pg, table, snapshot)
        for table, cols in definitions.items():
            if "id" not in cols:
                continue
            seq = await pg.fetchval("SELECT pg_get_serial_sequence($1,'id')", "public." + _q(table))
            if seq is None:
                continue
            previous = db.execute(
                "SELECT max(seq) FROM sqlite_sequence WHERE name=?", (table,)
            ).fetchone()[0]
            high = max(
                0, previous or 0, await pg.fetchval(f"SELECT max(id) FROM public.{_q(table)}") or 0
            )
            await pg.fetchval("SELECT setval($1::regclass,$2,$3)", seq, max(1, high), high > 0)
            if await pg.fetchval("SELECT nextval($1::regclass)", seq) != high + 1:
                raise RehearsalError("sequence_mismatch:" + table)
            if retain:
                # The probe consumed one value; leave that ID available to the future application.
                await pg.fetchval("SELECT setval($1::regclass,$2,$3)", seq, max(1, high), high > 0)
            next_ids[table] = high + 1
    except BaseException:
        await tx.rollback()
        raise
    if retain:
        await tx.commit()
    else:
        await tx.rollback()
    return verified, next_ids
