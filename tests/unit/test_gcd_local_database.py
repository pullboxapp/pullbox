"""Safety and bounded reads against the observed GCD SQLite schema."""

import asyncio
import sqlite3
import threading
import time

import pytest

from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.providers.metadata.gcd_local_database import (
    GcdDatabaseError,
    disk_read,
    open_readonly,
    validate_candidate,
)
from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery
from tests.api.test_gcd_local import gcd_dump


def test_external_read_compiles_bound_in_parameters(tmp_path):
    from sqlalchemy import select

    from pullbox.providers.metadata.gcd_local_database import ISSUE, rows

    path = gcd_dump(tmp_path / "gcd.db")
    with open_readonly(path, threading.Event(), time.monotonic() + 1) as db:
        assert [row[0] for row in rows(db, select(ISSUE.c.id).where(ISSUE.c.id.in_([10, 12])))] == [
            10,
            12,
        ]


def test_series_count_uses_series_index_not_low_selectivity_deleted_index(tmp_path):
    from sqlalchemy.dialects.sqlite import dialect

    from pullbox.providers.metadata.gcd_local import SERIES, series_query

    path = gcd_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX idx_gcd_issue_deleted ON gcd_issue(deleted)")
        query = (
            series_query().where(SERIES.c.id == 2999).compile(dialect=dialect(paramstyle="named"))
        )
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + str(query), query.params)]
    assert not any("idx_gcd_issue_deleted" in detail for detail in plan)
    assert any("issue_series" in detail for detail in plan)


async def test_validated_connection_cannot_write_and_new_columns_are_allowed(tmp_path):
    path = gcd_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE gcd_series ADD COLUMN future_column TEXT")
    snapshot = await validate_candidate(str(path))
    assert snapshot.signature[2] == path.stat().st_size
    with open_readonly(path, threading.Event(), time.monotonic() + 1) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM gcd_series")
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1


async def test_gcd_pages_are_complete_stable_and_search_input_is_bound(tmp_path):
    path = gcd_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,2999,?,'',?,0,NULL,'','','',32)",
            [(1000 + n, str(100 + n), 100 + n) for n in range(201)],
        )
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    profile = await adapter.series("2999")
    assert profile.data.issue_count == 204
    pages = [(await adapter.issues("2999", page=n)).data for n in (1, 2, 3)]
    assert [len(page.results) for page in pages] == [100, 100, 4]
    assert [page.next_page for page in pages] == [2, 3, None]
    assert len({row.external_id for page in pages for row in page.results}) == 204
    assert (await adapter.search(SeriesDiscoveryQuery(query="' OR 1=1 --"), 0)).results == []
    assert (await adapter.issue("13")).data is None  # Same-series variant is not a new issue.
    assert (await adapter.issue("10")).data.issue_number_text == "13a"


async def test_disk_cancellation_stops_and_drains_owned_work():
    started = threading.Event()
    stopped = threading.Event()

    def work(stop, deadline):
        started.set()
        stop.wait(2)
        stopped.set()
        return 1

    task = asyncio.create_task(disk_read(work, validation=True))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    with pytest.raises(GcdDatabaseError, match="busy"):
        await disk_read(work, validation=True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert await disk_read(lambda stop, deadline: 2, validation=True) == 2


async def test_disconnected_settings_request_cancels_validation(monkeypatch):
    from pullbox.services import gcd_local_activation as activation

    stopped = asyncio.Event()

    async def validate(_path):
        stopped.set()
        return None

    async def disconnected():
        return True

    monkeypatch.setattr(activation, "validate_candidate", validate)
    with pytest.raises(GcdDatabaseError, match="cancelled"):
        await activation.validate_for_request("/candidate.db", disconnected)
    assert stopped.is_set()


async def test_saturated_readers_report_temporary_unavailability(tmp_path, monkeypatch):
    from pullbox.providers.metadata import gcd_local_database as database
    from pullbox.schemas.metadata_sources import SourceStatus
    from pullbox.services.metadata_discovery import MetadataSourceError

    snapshot = await validate_candidate(str(gcd_dump(tmp_path / "gcd.db")))
    monkeypatch.setattr(database, "_READ_SLOTS", threading.BoundedSemaphore(0))
    with pytest.raises(MetadataSourceError) as failed:
        await GcdLocalSource(snapshot).check()
    assert failed.value.status is SourceStatus.UNAVAILABLE
