"""File handling must not change Mylar discovery or hide stale references."""

from __future__ import annotations

import zipfile
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import IssueType
from pullbox.models.library import LibraryRoot
from pullbox.services.import_path_identity import same_trusted_issue
from pullbox.services.import_scan_helpers import validate_discovered_files_safety
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_scan_pipeline import _load_mylar3_discovered_series
from pullbox.ui.import_review_summary import load_import_review_summary
from scripts.mylar3_import_fixture import create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


def _archive(path: Path, title: str, issue_id: int, year: int, start_year: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{title}</Series><Number>1</Number><Year>{year}</Year>"
            f"<Volume>{start_year}</Volume>"
            f"<Web>https://comicvine.gamespot.com/issue/4000-{issue_id}/</Web></ComicInfo>",
        )
        archive.writestr("01.jpg", b"first page")
        archive.writestr("02.jpg", b"second page")


@pytest.mark.parametrize("mode", list(ImportFileHandlingMode))
@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("missing_annual", [False, True])
async def test_scan_counts_files_separately_and_reconciles_stale_paths(
    db_session, tmp_path, mode, collection, missing_annual
):
    title = "Dawn of X" if collection else "Alpha Flight"
    year = 2020 if collection else 2005
    folder = tmp_path / f"{title} ({2020 if collection else 2004})"
    actual_name = f"{title} {'v01' if collection else '001'} ({year}).cbz"
    actual = folder / actual_name
    _archive(actual, title, 737192, year, 2020 if collection else 2004)
    recorded_name = f"{title} {'v01' if collection else '01'} ({year}) (Digital-Empire).cbz"
    missing_name = f"{title} {'Annual 001' if missing_annual else '002'} ({year}).cbz"
    missing_record = {
        "ComicID": "124996",
        "IssueID": "737193",
        "Issue_Number": "1" if missing_annual else "2",
        "Location": missing_name,
        "IssueDate": f"{year}-03-01",
    }
    db = tmp_path / "mylar.db"
    create_mylar3_db(
        db,
        series=[
            {
                "ComicID": "124996",
                "ComicName": title,
                "ComicYear": str(2020 if collection else 2004),
                "ComicLocation": str(folder),
            }
        ],
        issues=[
            {
                "ComicID": "124996",
                "IssueID": "737192",
                "Issue_Number": "1",
                "Location": recorded_name,
                "IssueDate": f"{year}-02-01",
            },
        ]
        + ([] if missing_annual else [missing_record]),
        annuals=[
            {
                **missing_record,
                "ReleaseComicID": "153726",
                "ReleaseComicName": f"{title} Annual",
            }
        ]
        if missing_annual
        else [],
    )
    before = {path: path.read_bytes() for path in (db, actual)}
    db_session.add(LibraryRoot(name="Source", path=str(tmp_path), allow_managed_writes=False))
    job = ImportJob(
        source_path=str(db),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        file_handling_mode=mode,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    events = []

    async def log_event(*args, **kwargs):
        events.append(kwargs)

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=Mylar3Reader,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
        validate_discovered_files_safety=validate_discovered_files_safety,
        materialize_discovered_scan_results=materialize_discovered_scan_results,
    )
    files = list(await db_session.scalars(select(ImportedFile).order_by(ImportedFile.file_path)))
    assert {file.file_name for file in files} == {actual_name, missing_name}
    actual_row = next(file for file in files if file.file_name == actual_name)
    missing_row = next(file for file in files if file.file_name == missing_name)
    assert actual_row.comicvine_issue_id == 737192
    assert actual_row.diagnostics["source_metadata"]["mylar3_path_reconciliation"][
        "recorded_path"
    ] == str(folder / recorded_name)
    assert missing_row.status == ImportedFileStatus.SAFETY_BLOCKED
    assert missing_row.diagnostics["safety_block"]["code"] == "source_missing"
    series = list(await db_session.scalars(select(ImportedSeries)))
    assert len(series) == (2 if missing_annual else 1)
    if missing_annual:
        assert missing_row.import_series_id != actual_row.import_series_id
        assert missing_row.diagnostics["source_issue_type"] == IssueType.ANNUAL.value
    assert job.scan_total_files == 2  # Durable record counter remains backward compatible.
    job.status = ImportJobStatus.REVIEW
    summary = await load_import_review_summary(db_session, job)
    assert summary["files_total"] == 2
    assert summary.get("files_present") == 1
    assert summary.get("files_missing_references") == 1
    assert summary["files_safety_blocked"] == 0
    assert {path: path.read_bytes() for path in before} == before
    batch = next(event for event in events if "files_present" in event)
    assert batch["files_present"] == 1
    assert batch["missing_references"] == 1
    assert batch["reconciled_references"] == 1
    assert batch["file_handling_mode"] == mode.value


@pytest.mark.parametrize(
    "conflict", ["year", "annual", "id", "number", "volume_without_evidence", "volume_number"]
)
def test_reconciliation_keeps_conflicting_issue_evidence(tmp_path, conflict):
    actual_path = tmp_path / "Alpha Flight (2004)" / "Alpha Flight 001 (2005).cbz"
    _archive(actual_path, "Alpha Flight", 123, 2005, 2004)
    actual = SourceMetadataExtractor().from_path(actual_path)
    recorded = SourceMetadata(
        original_title="Alpha Flight 01 (2005) (Digital).cbz",
        series_name="Alpha Flight",
        issue_number=1,
        comicvine_issue_id=123,
        comicvine_series_id=321,
        signals={"comicvine_issue_id": MetadataSignal.MYLAR3},
        diagnostics={"mylar3_issue": {"release_date": "2005-01-01"}},
    )
    assert same_trusted_issue(recorded, actual)
    if conflict == "year":
        # Matching series year must not hide a contradictory publication year.
        recorded = replace(recorded, diagnostics={"mylar3_issue": {"release_date": "2004-01-01"}})
    elif conflict == "annual":
        actual = replace(actual, issue_type=IssueType.ANNUAL)
    elif conflict == "id":
        actual = replace(actual, comicvine_issue_id=124)
    elif conflict == "number":
        actual = replace(actual, issue_number=2)
    else:
        actual = replace(actual, issue_type=IssueType.VOLUME)
        if conflict == "volume_number":
            recorded = replace(recorded, original_title="Alpha Flight v02 (2005).cbz")
    assert not same_trusted_issue(recorded, actual)
