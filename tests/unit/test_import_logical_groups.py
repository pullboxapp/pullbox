"""Tests for import logical-series grouping helper shims."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_service() -> ImportService:
    return ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.MATCHING,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    name: str = "Absolute Wonder Woman",
    cv_id: int | None = 12345,
    status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    series_id: int | None = None,
    selected_for_import: bool = False,
) -> ImportedSeries:
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=name,
        raw_year=2025,
        status=status,
        cv_id=cv_id,
        cv_match_score=0.95 if cv_id is not None else None,
        cv_match_method="exact_title_year" if cv_id is not None else None,
        series_id=series_id,
        file_count=1,
        files_total=1,
        selected_for_import=selected_for_import,
    )
    session.add(series)
    await session.flush()
    return series


def _make_imported_file(
    job: ImportJob,
    series: ImportedSeries,
    *,
    status: ImportedFileStatus = ImportedFileStatus.CONFLICT,
    diagnostics: dict[str, object] | None = None,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/tmp/comics/{series.raw_series_name}.cbz",
        file_name=f"{series.raw_series_name}.cbz",
        file_size=1024,
        file_format="cbz",
        status=status,
        matched_issue_cv_id=456,
        match_confidence="high",
        match_method="issue_number",
        conflict_group_id=99,
        duplicate_group_id=88,
        include_in_import=True,
        is_preferred=True,
        error_message="stale error",
        diagnostics=diagnostics
        or {
            "source_issue_type": "issue",
            "comicvine_series_id": 12345,
            "drop_me": True,
        },
    )


async def test_reclassify_matched_series_duplicates_marks_existing_cv_match(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    existing = Series(
        title="Absolute Wonder Woman",
        sort_title="absolute wonder woman",
        year_start=2025,
        comicvine_id=12345,
    )
    db_session.add(existing)
    await db_session.flush()
    imported = await _create_imported_series(db_session, job, cv_id=12345)

    count = await service._reclassify_matched_series_duplicates(db_session, job)

    assert count == 1
    assert imported.status == ImportSeriesStatus.DUPLICATE
    assert imported.series_id == existing.id
    assert imported.diagnostics["kind"] == "duplicate_series"
    assert imported.diagnostics["duplicate_reason"] == "cv_id"
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_dedup_post_match_cv_id_match",
            )
        )
    ).scalar_one()
    assert log.data["existing_series_id"] == existing.id


async def test_reclassify_matched_series_flags_owned_reversed_crossover_candidate(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    root = LibraryRoot(name="Main", path="/library")
    db_session.add(root)
    await db_session.flush()
    owned_series = Series(
        title="Marvel/DC: Spider-Man/Superman",
        sort_title="marvel dc spider man superman",
        year_start=2026,
        comicvine_id=171592,
        status=SeriesStatus.CONTINUING,
        issue_count=1,
        series_type=SeriesType.STANDARD,
        library_root_id=root.id,
    )
    db_session.add(owned_series)
    await db_session.flush()
    owned_issue = Issue(
        series_id=owned_series.id,
        comicvine_id=1163631,
        issue_number=1.0,
        release_date=datetime(2026, 6, 1, tzinfo=UTC).date(),
        status=IssueStatus.OWNED,
    )
    db_session.add(owned_issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path="/library/MarvelDC - Spider-ManSuperman (2026) #001.cbz",
            file_name="MarvelDC - Spider-ManSuperman (2026) #001.cbz",
            file_size=286277917,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=owned_issue.id,
            library_root_id=root.id,
        )
    )
    imported = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Superman Spider-Man",
        raw_year=2026,
        status=ImportSeriesStatus.MATCHED,
        cv_id=171099,
        cv_title="DC/Marvel: Superman/Spider-Man",
        cv_year=2026,
        cv_issue_count=1,
        cv_match_score=0.9342,
        cv_match_method="fuzzy_title",
        file_count=1,
    )
    db_session.add(imported)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported.id,
        file_path="/tmp/comics/Superman Spider-Man 01 (2026).cbr",
        file_name="Superman Spider-Man 01 (2026).cbr",
        file_size=203251840,
        file_format="cbr",
        parsed_series="Superman Spider-Man",
        parsed_issue_number=1.0,
        parsed_year=2026,
        has_comicinfo=False,
        status=ImportedFileStatus.PENDING,
    )
    db_session.add(imported_file)
    await db_session.flush()

    duplicate_count = await service._reclassify_matched_series_duplicates(db_session, job)

    assert duplicate_count == 0
    assert imported.status == ImportSeriesStatus.NO_MATCH
    assert imported.selected_for_import is False
    assert imported.cv_id is None
    assert imported.diagnostics["kind"] == "series_conflict"
    assert imported.diagnostics["reason"] == "library_owned_ambiguous_candidate"
    assert imported.diagnostics["selected_candidate"]["cv_id"] == 171099
    assert imported.diagnostics["competing_candidate"]["cv_id"] == 171592
    assert imported.diagnostics["competing_candidate"]["existing_series_id"] == owned_series.id
    assert imported_file.status == ImportedFileStatus.NO_MATCH
    assert imported_file.diagnostics["kind"] == "series_conflict_file"
    assert imported_file.diagnostics["reason"] == "library_owned_ambiguous_candidate"


async def test_reclassify_matched_series_respects_manual_reversed_crossover_choice(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    root = LibraryRoot(name="Main", path="/library")
    db_session.add(root)
    await db_session.flush()
    owned_series = Series(
        title="Marvel/DC: Spider-Man/Superman",
        sort_title="marvel dc spider man superman",
        year_start=2026,
        comicvine_id=171592,
        status=SeriesStatus.CONTINUING,
        issue_count=1,
        series_type=SeriesType.STANDARD,
        library_root_id=root.id,
    )
    db_session.add(owned_series)
    await db_session.flush()
    owned_issue = Issue(
        series_id=owned_series.id,
        comicvine_id=1163631,
        issue_number=1.0,
        release_date=datetime(2026, 6, 1, tzinfo=UTC).date(),
        status=IssueStatus.OWNED,
    )
    db_session.add(owned_issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path="/library/MarvelDC - Spider-ManSuperman (2026) #001.cbz",
            file_name="MarvelDC - Spider-ManSuperman (2026) #001.cbz",
            file_size=286277917,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=owned_issue.id,
            library_root_id=root.id,
        )
    )
    imported = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Superman Spider-Man",
        raw_year=2026,
        status=ImportSeriesStatus.MATCHED,
        cv_id=171099,
        user_selected_cv_id=171099,
        cv_title="DC/Marvel: Superman/Spider-Man",
        cv_year=2026,
        cv_issue_count=1,
        cv_match_score=1.0,
        cv_match_method="user_override",
        file_count=1,
    )
    db_session.add(imported)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imported.id,
            file_path="/tmp/comics/Superman Spider-Man 01 (2026).cbr",
            file_name="Superman Spider-Man 01 (2026).cbr",
            file_size=203251840,
            file_format="cbr",
            parsed_series="Superman Spider-Man",
            parsed_issue_number=1.0,
            parsed_year=2026,
            has_comicinfo=False,
            status=ImportedFileStatus.PENDING,
        )
    )
    await db_session.flush()

    duplicate_count = await service._reclassify_matched_series_duplicates(db_session, job)

    assert duplicate_count == 0
    assert imported.status == ImportSeriesStatus.MATCHED
    assert imported.cv_id == 171099


async def test_logical_group_series_ids_returns_current_matching_group(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    first = await _create_imported_series(db_session, job, name="Chicken Devil", cv_id=139451)
    second = await _create_imported_series(db_session, job, name="Chicken Devils", cv_id=139451)
    await _create_imported_series(db_session, job, name="Abattoir", cv_id=36339)
    group_key = service._logical_series_group_key(first, prefer_resolved_cv_only=True)
    assert group_key is not None

    ids = await service._logical_group_series_ids(
        db_session,
        job.id,
        group_key,
        prefer_resolved_cv_only=True,
    )

    assert ids == [first.id, second.id]


async def test_consolidate_logical_groups_preserves_selected_canonical_match(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    selected = await _create_imported_series(
        db_session,
        job,
        name="Chicken Devil",
        cv_id=139451,
        selected_for_import=True,
    )
    sibling = await _create_imported_series(
        db_session,
        job,
        name="Chicken Devils",
        cv_id=139451,
    )

    canonical_by_series_id = await service._consolidate_logical_series_groups(
        db_session,
        job,
        prefer_resolved_cv_only=True,
    )

    assert canonical_by_series_id[selected.id] == selected.id
    assert canonical_by_series_id[sibling.id] == selected.id
    assert selected.selected_for_import is True


async def test_reset_series_group_files_clears_review_state_but_keeps_source_diagnostics(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series = await _create_imported_series(db_session, job)
    file_row = _make_imported_file(job, series)
    db_session.add(file_row)
    await db_session.flush()

    await service._reset_series_group_files(db_session, job_id=job.id, series_ids=[series.id])

    assert file_row.status == ImportedFileStatus.PENDING
    assert file_row.include_in_import is False
    assert file_row.matched_issue_id is None
    assert file_row.matched_issue_cv_id is None
    assert file_row.match_confidence is None
    assert file_row.match_method is None
    assert file_row.conflict_group_id is None
    assert file_row.duplicate_group_id is None
    assert file_row.duplicate_of_file_id is None
    assert file_row.is_preferred is False
    assert file_row.error_message is None
    assert file_row.diagnostics == {
        "source_issue_type": "issue",
        "comicvine_series_id": 12345,
    }
