"""Tests for import scan materialization helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import IssueType
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
    )
    session.add(job)
    await session.flush()
    return job


def _discovered_file(
    *,
    name: str = "Absolute Wonder Woman 019.cbz",
) -> DiscoveredFile:
    return DiscoveredFile(
        file_path=f"/tmp/comics/Absolute Wonder Woman/{name}",
        file_name=name,
        file_size=12345,
        file_format="cbz",
        parsed_series="Absolute Wonder Woman",
        parsed_issue_number=19.0,
        parsed_year=2025,
        parsed_publisher="DC",
        has_comicinfo=True,
        comicvine_issue_id=123456,
        issue_number_raw="019",
        issue_type=IssueType.ISSUE,
        comicvine_series_id=98765,
        series_status="Continuing",
        issue_count_hint=24,
        metadata_signals={"series": "comicinfo", "issue": "filename"},
        metadata_diagnostics={"title": "Absolute Wonder Woman"},
        source_signature={
            "schema_version": 1,
            "resolved_path": f"/tmp/comics/Absolute Wonder Woman/{name}",
            "size": 12345,
            "mtime_ns": 123456789,
            "device": 1,
            "inode": 2,
        },
        source_folder_cohort_key="Absolute Wonder Woman",
        source_ordinal=19,
    )


async def test_materialize_discovered_scan_results_persists_series_and_files(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    discovered = DiscoveredSeries(
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        raw_publisher="DC",
        file_count=1,
        sample_paths=["/tmp/comics/Absolute Wonder Woman/Absolute Wonder Woman 019.cbz"],
        source_folder="/tmp/comics/Absolute Wonder Woman",
        source_folder_relative="Absolute Wonder Woman",
        files=[_discovered_file()],
        has_files=True,
        mylar3_cv_id=111,
        comicinfo_cv_id=222,
        diagnostics={"source": "scanner"},
    )

    pairs = await materialize_discovered_scan_results(db_session, job, [discovered])

    assert len(pairs) == 1
    assert pairs[0][0] is discovered
    assert job.series_found == 1
    assert job.scan_completed_at is not None

    series = (
        (
            await db_session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
            )
        )
        .scalars()
        .one()
    )
    assert pairs[0][1] is series
    assert series.raw_series_name == "Absolute Wonder Woman"
    assert series.raw_year == 2024
    assert series.raw_publisher == "DC"
    assert series.sample_paths == [
        "/tmp/comics/Absolute Wonder Woman/Absolute Wonder Woman 019.cbz"
    ]
    assert series.source_folder == "/tmp/comics/Absolute Wonder Woman"
    assert series.status == ImportSeriesStatus.PENDING
    assert series.cv_id == 111
    assert series.cv_match_method == "mylar3_cv_id"
    assert series.diagnostics == {"source": "scanner"}
    assert series.files_total == 1

    file_row = (
        (await db_session.execute(select(ImportedFile).where(ImportedFile.import_job_id == job.id)))
        .scalars()
        .one()
    )
    assert file_row.import_series_id == series.id
    assert file_row.file_name == "Absolute Wonder Woman 019.cbz"
    assert file_row.file_format == "cbz"
    assert file_row.parsed_series == "Absolute Wonder Woman"
    assert file_row.parsed_issue_number == 19.0
    assert file_row.parsed_year == 2025
    assert file_row.has_comicinfo is True
    assert file_row.comicvine_issue_id == 123456
    assert file_row.issue_number_raw == "019"
    assert file_row.source_folder_cohort_key == "Absolute Wonder Woman"
    assert file_row.source_ordinal == 19
    assert file_row.status == ImportedFileStatus.PENDING
    assert file_row.source_signature == {
        "schema_version": 1,
        "resolved_path": ("/tmp/comics/Absolute Wonder Woman/Absolute Wonder Woman 019.cbz"),
        "size": 12345,
        "mtime_ns": 123456789,
        "device": 1,
        "inode": 2,
    }
    assert file_row.diagnostics == {
        "source_issue_type": "issue",
        "comicvine_series_id": 98765,
        "series_status": "Continuing",
        "issue_count_hint": 24,
        "metadata_signals": {"series": "comicinfo", "issue": "filename"},
        "source_metadata": {"title": "Absolute Wonder Woman"},
    }


async def test_materialize_discovered_scan_results_persists_safety_blocked_files(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    blocked_file = _discovered_file(name="Absolute Wonder Woman Omnibus.cbz")
    blocked_file.metadata_diagnostics = {
        "title": "Absolute Wonder Woman",
        "file_safety": {
            "kind": "file_safety_blocked",
            "reason": "Archive decompressed size exceeds limit",
            "details": [blocked_file.file_path],
        },
    }
    discovered = DiscoveredSeries(
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        raw_publisher="DC",
        file_count=1,
        sample_paths=[blocked_file.file_path],
        source_folder="/tmp/comics/Absolute Wonder Woman",
        source_folder_relative="Absolute Wonder Woman",
        files=[blocked_file],
        has_files=True,
    )

    await materialize_discovered_scan_results(db_session, job, [discovered])

    file_row = (
        (await db_session.execute(select(ImportedFile).where(ImportedFile.import_job_id == job.id)))
        .scalars()
        .one()
    )
    assert file_row.status == ImportedFileStatus.SAFETY_BLOCKED
    assert file_row.include_in_import is False
    assert file_row.error_message == "Archive decompressed size exceeds limit"
    assert file_row.diagnostics["source_metadata"] == {"title": "Absolute Wonder Woman"}
    assert file_row.diagnostics["safety_block"] == {
        "kind": "file_safety_blocked",
        "reason": "Archive decompressed size exceeds limit",
        "details": [blocked_file.file_path],
    }


async def test_materialize_non_fitting_layout_files_as_explicit_review_rows(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    review_file = _discovered_file(name="Batman 001.cbz")
    review_file.metadata_diagnostics = {
        "source_layout": {
            "fit": False,
            "fallback_used": False,
            "review_required": True,
            "review_reason": "selected_layout_no_match",
            "relative_path": "Batman (2011)/Batman 001.cbz",
        }
    }
    discovered = DiscoveredSeries(
        raw_series_name="Batman",
        raw_year=2011,
        raw_publisher=None,
        file_count=1,
        sample_paths=[review_file.file_path],
        source_folder="/tmp/comics/Batman (2011)",
        source_folder_relative="Batman (2011)",
        files=[review_file],
        has_files=True,
    )

    pairs = await materialize_discovered_scan_results(db_session, job, [discovered])

    series_row = pairs[0][1]
    file_row = (
        (await db_session.execute(select(ImportedFile).where(ImportedFile.import_job_id == job.id)))
        .scalars()
        .one()
    )
    assert series_row.status == ImportSeriesStatus.NO_MATCH
    assert series_row.diagnostics["kind"] == "source_layout_review"
    assert series_row.diagnostics["reason"] == "selected_layout_no_match"
    assert file_row.status == ImportedFileStatus.NO_MATCH
    assert file_row.include_in_import is False
    assert file_row.diagnostics["kind"] == "source_layout_review"
    assert file_row.diagnostics["rejection_reason"] == (
        "This file does not fit the selected source layout. Review its series before importing."
    )
    assert file_row.diagnostics["source_metadata"]["source_layout"]["relative_path"] == (
        "Batman (2011)/Batman 001.cbz"
    )


async def test_materialize_incompatible_mylar_path_as_series_review(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    discovered = DiscoveredSeries(
        raw_series_name="Batman",
        raw_year=2011,
        raw_publisher="DC Comics",
        file_count=0,
        sample_paths=[],
        source_folder="",
        source_folder_relative="/comics/Batman",
        files=[],
        has_files=False,
        mylar3_cv_id=42721,
        diagnostics={
            "kind": "mylar3_path_incompatible",
            "reason": "unmapped_path",
            "rejection_reason": (
                "The Mylar comic folder is not available through the configured path mappings."
            ),
            "mylar3_path": {
                "status": "unmapped",
                "mapping_applied": False,
            },
        },
    )

    pairs = await materialize_discovered_scan_results(db_session, job, [discovered])

    series_row = pairs[0][1]
    assert series_row.status == ImportSeriesStatus.NO_MATCH
    assert series_row.cv_id == 42721
    assert series_row.cv_match_method == "mylar3_cv_id"
    assert series_row.diagnostics["kind"] == "mylar3_path_incompatible"
    assert series_row.diagnostics["reason"] == "unmapped_path"


async def test_materialize_mixed_layout_keeps_only_non_fitting_file_in_review(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    fitting_file = _discovered_file(name="Batman 001.cbz")
    fitting_file.metadata_diagnostics = {
        "source_layout": {
            "fit": True,
            "fallback_used": False,
            "relative_path": "DC Comics/Batman/Batman 001.cbz",
        }
    }
    review_file = _discovered_file(name="Batman Special.cbz")
    review_file.metadata_diagnostics = {
        "source_layout": {
            "fit": False,
            "fallback_used": False,
            "review_required": True,
            "review_reason": "selected_layout_no_match",
            "relative_path": "Loose/Batman Special.cbz",
        }
    }
    discovered = DiscoveredSeries(
        raw_series_name="Batman",
        raw_year=2011,
        raw_publisher="DC Comics",
        file_count=2,
        sample_paths=[fitting_file.file_path, review_file.file_path],
        source_folder="/tmp/comics/DC Comics/Batman",
        source_folder_relative="DC Comics/Batman",
        files=[fitting_file, review_file],
        has_files=True,
    )

    pairs = await materialize_discovered_scan_results(db_session, job, [discovered])

    assert pairs[0][1].status == ImportSeriesStatus.PENDING
    files = (
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_job_id == job.id)
                .order_by(ImportedFile.file_name)
            )
        )
        .scalars()
        .all()
    )
    assert {row.file_name: row.status for row in files} == {
        "Batman 001.cbz": ImportedFileStatus.PENDING,
        "Batman Special.cbz": ImportedFileStatus.NO_MATCH,
    }


async def test_materialize_discovered_scan_results_uses_comicinfo_cv_when_no_mylar_id(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    discovered = DiscoveredSeries(
        raw_series_name="Helen of Wyndhorn",
        raw_year=2024,
        raw_publisher="Dark Horse",
        file_count=0,
        sample_paths=[],
        source_folder="/tmp/comics/Helen of Wyndhorn",
        source_folder_relative="Helen of Wyndhorn",
        files=[],
        has_files=False,
        comicinfo_cv_id=333,
    )

    pairs = await materialize_discovered_scan_results(db_session, job, [discovered])

    series = pairs[0][1]
    assert series.cv_id == 333
    assert series.cv_match_method == "comicinfo_cv_id"
    assert series.has_files is False
    assert series.files_total == 0
    assert job.series_found == 1
