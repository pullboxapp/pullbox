"""Compare the same ordered, normalized rows during transfer and at first start."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from pullbox.models import Base

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

_BATCH = 128
_BUFFER_BYTES = 2 * 1024 * 1024


class RehearsalError(Exception):
    """Only static diagnostics and known schema identifiers may cross this boundary."""


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value(value: Any, kind: str) -> Any:
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


def _hash_row(digest: Any, row: Iterable[object]) -> int:
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


def _encode_temporal(value: object) -> str:
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(UTC)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("unsupported comparison type")


async def table_snapshot(
    db: sqlite3.Connection,
    pg: Any,
    name: str,
    columns: dict[str, str],
    expected_count: int | None,
    *,
    copy: bool,
) -> TableSnapshot:
    sqlite_cols = db.execute(
        "SELECT name,pk FROM pragma_table_info(?) ORDER BY cid", (name,)
    ).fetchall()
    cols = [col for col, _ in sqlite_cols]
    pk = [col for col, pos in sorted(sqlite_cols, key=lambda item: item[1]) if pos]
    model = Base.metadata.tables.get(name)
    if (
        model is None
        or not pk
        or set(cols) != set(columns)
        or set(cols) != set(model.columns.keys())
    ):
        raise RehearsalError("column_layout:" + name)
    qualified = "public." + _q(name)
    keys = ",".join(map(_q, cols))
    pg_order = ",".join(
        _q(c) + (' COLLATE "C"' if columns[c] in {"character varying", "text"} else "") for c in pk
    )
    query = select(*(model.c[c] for c in cols)).order_by(
        *(model.c[c].collate("BINARY") for c in pk)
    )
    source = db.execute(str(query.compile(dialect=sqlite.dialect())))
    id_index = cols.index("id") if "id" in cols else None
    high_id = 0
    left = hashlib.sha256()
    count, buffered, largest = 0, 0, 1
    rows = []
    for record in source:
        if id_index is not None and type(record[id_index]) is int:
            high_id = max(high_id, record[id_index])
        row = []
        for col, value in zip(cols, record, strict=True):
            try:
                row.append(_value(value, columns[col]))
            except (TypeError, ValueError, OverflowError):
                raise RehearsalError(f"unrepresentable_value:{name}.{col}") from None
        size = _hash_row(left, row)
        largest = max(largest, size)
        count += 1
        if not copy:
            continue
        buffered += size
        rows.append(tuple(row))
        if len(rows) >= _BATCH or buffered >= _BUFFER_BYTES:
            await pg.copy_records_to_table(name, schema_name="public", columns=cols, records=rows)
            rows.clear()
            buffered = 0
    if rows:
        await pg.copy_records_to_table(name, schema_name="public", columns=cols, records=rows)
    if expected_count is not None and count != expected_count:
        raise RehearsalError("source_count:" + name)
    return TableSnapshot(
        count,
        left.digest(),
        f"SELECT {keys} FROM {qualified} ORDER BY {pg_order}",
        max(1, min(_BATCH, _BUFFER_BYTES // largest)),
        high_id,
    )


@dataclass(frozen=True)
class TableSnapshot:
    count: int
    digest: bytes
    target_query: str
    prefetch: int = _BATCH
    high_id: int = 0


async def _verify_table(pg: Any, name: str, snapshot: TableSnapshot) -> int:
    present, right = 0, hashlib.sha256()
    async for row in pg.cursor(snapshot.target_query, prefetch=snapshot.prefetch):
        _hash_row(right, row)
        present += 1
    if present != snapshot.count or snapshot.digest != right.digest():
        raise RehearsalError("row_mismatch:" + name)
    return present
