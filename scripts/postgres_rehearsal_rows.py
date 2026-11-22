"""Copy and compare application rows inside a newly generated throwaway database."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pullbox.models import Base
from pullbox.services.database_migration_preflight import SOURCE_REVISION

_BATCH = 128
_BUFFER_BYTES = 2 * 1024 * 1024


class RehearsalError(Exception):
    """Only static diagnostics and known schema identifiers may cross this boundary."""


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _value(value, kind):
    if value is None:
        return None
    if kind == "boolean" and type(value) is int and value in (0, 1):
        return bool(value)
    if kind in {"integer", "smallint", "bigint"} and type(value) is int:
        return value
    if kind == "double precision" and type(value) in (int, float):
        converted = float(value)
        if math.isfinite(converted) and converted == value:
            return converted
    if type(value) is str:
        if kind in {"character varying", "text", "USER-DEFINED", "json"}:
            # asyncpg's JSON codec expects text. Preserve JSON null separately from SQL NULL.
            return value
        if kind == "date":
            return date.fromisoformat(value)
        if kind in {"timestamp with time zone", "timestamp without time zone"}:
            if not re.fullmatch(
                r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d{1,6}0*)?(?:Z|[+-]\d\d:\d\d)?", value
            ):
                raise ValueError("unrepresentable timestamp")
            stamp = datetime.fromisoformat(value)
            if kind == "timestamp with time zone":
                return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
            if stamp.tzinfo is None:
                return stamp
    raise ValueError("unrepresentable value")


def _hash_row(digest, row):
    encoded = json.dumps(
        list(row),
        ensure_ascii=False,
        default=_encode_temporal,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    return len(encoded)


def _encode_temporal(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(UTC)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("unsupported comparison type")


async def _copy_table(db, pg, name, columns, expected_count):
    sqlite_cols = db.execute(
        "SELECT name,pk FROM pragma_table_info(?) ORDER BY cid", (name,)
    ).fetchall()
    cols = [col for col, _ in sqlite_cols]
    pk = [col for col, pos in sorted(sqlite_cols, key=lambda item: item[1]) if pos]
    if not pk or set(cols) != set(columns):
        raise RehearsalError("column_layout:" + name)
    qualified = "public." + _q(name)
    keys = ",".join(map(_q, cols))
    sqlite_order = ",".join(_q(c) + " COLLATE BINARY" for c in pk)
    pg_order = ",".join(
        _q(c) + (' COLLATE "C"' if columns[c] in {"character varying", "text"} else "") for c in pk
    )
    source = db.execute(f"SELECT {keys} FROM {_q(name)} ORDER BY {sqlite_order}")
    left = hashlib.sha256()
    count, buffered, largest = 0, 0, 1
    rows = []
    for record in source:
        row = []
        for col, value in zip(cols, record, strict=True):
            try:
                row.append(_value(value, columns[col]))
            except (TypeError, ValueError, OverflowError):
                raise RehearsalError(f"unrepresentable_value:{name}.{col}") from None
        size = _hash_row(left, row)
        largest = max(largest, size)
        buffered += size
        count += 1
        rows.append(tuple(row))
        if len(rows) >= _BATCH or buffered >= _BUFFER_BYTES:
            await pg.copy_records_to_table(name, schema_name="public", columns=cols, records=rows)
            rows.clear()
            buffered = 0
    if rows:
        await pg.copy_records_to_table(name, schema_name="public", columns=cols, records=rows)
    if count != expected_count:
        raise RehearsalError("source_count:" + name)
    return TableSnapshot(
        count,
        left.digest(),
        f"SELECT {keys} FROM {qualified} ORDER BY {pg_order}",
        max(1, min(_BATCH, _BUFFER_BYTES // largest)),
    )


@dataclass(frozen=True)
class TableSnapshot:
    count: int
    digest: bytes
    target_query: str
    prefetch: int = _BATCH


async def _verify_table(pg, name, snapshot):
    present, right = 0, hashlib.sha256()
    async for row in pg.cursor(snapshot.target_query, prefetch=snapshot.prefetch):
        _hash_row(right, row)
        present += 1
    if present != snapshot.count or snapshot.digest != right.digest():
        raise RehearsalError("row_mismatch:" + name)
    return present


async def compare_rows(db, pg, counts):
    """Caller owns the isolated target; never commit the trial or disable FK triggers."""
    import asyncpg

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
                snapshots[table] = await _copy_table(
                    db, pg, table, definitions[table], counts[table]
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
            next_ids[table] = high + 1
    finally:
        await tx.rollback()
    return verified, next_ids
