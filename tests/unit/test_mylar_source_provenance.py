"""Optional Mylar schema version is diagnostic evidence, not an admission gate."""

import sqlite3

import pytest

from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.services.import_scan_pipeline import _load_mylar3_discovered_series
from scripts.mylar3_import_fixture import create_mylar3_db


@pytest.mark.parametrize(
    "variant,value,expected",
    [
        ("value", 11, "11"),
        ("value", "9999", "9999"),
        ("missing", None, "unknown"),
        ("column_missing", None, "unknown"),
        ("value", None, "unknown"),
        ("value", "", "unknown"),
        ("value", "not-a-version", "unknown"),
        ("value", "secret\nvalue", "unknown"),
    ],
)
async def test_optional_mylar_version_is_read_only_and_capability_based(
    tmp_path, variant, value, expected
):
    source = tmp_path / "mylar.db"
    create_mylar3_db(source)
    with sqlite3.connect(source) as conn:
        if variant == "value":
            conn.execute("CREATE TABLE mylar_info (DatabaseVersion)")
            conn.execute("INSERT INTO mylar_info VALUES (?)", (value,))
        elif variant == "column_missing":
            conn.execute("CREATE TABLE mylar_info (something_else)")
    before = source.read_bytes()
    reader = Mylar3Reader(source)
    metadata = await reader.read_import_metadata()
    snapshot = await reader.read_snapshot()
    assert getattr(metadata, "database_version", None) == expected
    assert getattr(snapshot, "database_version", None) == expected
    assert metadata.series_count == 0
    assert snapshot.series == ()
    assert source.read_bytes() == before


async def test_mylar_source_version_is_in_durable_import_diagnostics(db_session, tmp_path):
    source = tmp_path / "mylar.db"
    create_mylar3_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE mylar_info (DatabaseVersion)")
        conn.execute("INSERT INTO mylar_info VALUES (11)")
    job = ImportJob(
        source_path=str(source),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    events = []

    async def log_event(_session, _job_id, _level, event, **details):
        events.append((event, details))

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=Mylar3Reader,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
    )
    provenance = [details for event, details in events if event == "mylar3_source_provenance"]
    assert len(provenance) == 1
    assert provenance[0]["database_version"] == "11"
    assert provenance[0]["series_count"] == 0
    assert "config_values" not in provenance[0]
