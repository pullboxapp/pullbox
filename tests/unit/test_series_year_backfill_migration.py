"""Unit tests for the ended-series year_end data repair migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text


def _load_migration_module():  # type: ignore[no-untyped-def]
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "v7q8r9s0t123_backfill_ended_series_year_end.py"
    )
    spec = importlib.util.spec_from_file_location("pb_year_end_backfill_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coerce_year_accepts_date_like_values() -> None:
    module = _load_migration_module()

    assert module._coerce_year("2014-08-27") == 2014
    assert module._coerce_year(" 2020 ") == 2020
    assert module._coerce_year(None) is None


def test_upgrade_backfills_only_ended_series_missing_year_end(monkeypatch, request) -> None:  # type: ignore[no-untyped-def]
    module = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    request.addfinalizer(engine.dispose)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE series ("
                "id INTEGER PRIMARY KEY, "
                "year_start INTEGER, "
                "year_end INTEGER, "
                "status TEXT)"
            )
        )
        conn.execute(
            text("CREATE TABLE issues (series_id INTEGER, release_date DATE, store_date DATE)")
        )
        conn.execute(
            text(
                "INSERT INTO series (id, year_start, year_end, status) VALUES "
                "(1, 2014, NULL, 'ENDED'), "
                "(2, 2020, NULL, 'ENDED'), "
                "(3, 2018, 2019, 'ENDED'), "
                "(4, 2016, NULL, 'CONTINUING')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO issues (series_id, release_date, store_date) VALUES "
                "(1, '2014-08-27', NULL), "
                "(1, NULL, '2014-09-01')"
            )
        )
        monkeypatch.setattr(module.op, "get_bind", lambda: conn)

        module.upgrade()

        rows = conn.execute(text("SELECT id, year_end FROM series ORDER BY id")).fetchall()

    assert rows == [(1, 2014), (2, 2020), (3, 2019), (4, None)]
