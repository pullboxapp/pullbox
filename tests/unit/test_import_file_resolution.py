"""Tests for import file selection and issue-resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series, SeriesType
from pullbox.services.import_file_resolution import (
    load_importable_files,
    load_issue_lookup_for_series,
    resolve_import_file_issue,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_series_with_issues(session: AsyncSession) -> tuple[Series, list[Issue]]:
    series = Series(
        title="Absolute Wonder Woman",
        sort_title="absolute wonder woman",
        year_start=2024,
        comicvine_id=165732,
        series_type=SeriesType.STANDARD,
    )
    session.add(series)
    await session.flush()

    issues = [
        Issue(
            series_id=series.id,
            issue_number=19.0,
            comicvine_id=100019,
            title="Issue 19",
            status=IssueStatus.WANTED,
        ),
        Issue(
            series_id=series.id,
            issue_number=20.0,
            comicvine_id=100020,
            title="Issue 20",
            status=IssueStatus.WANTED,
        ),
        Issue(
            series_id=series.id,
            issue_number=21.0,
            comicvine_id=100021,
            title="Issue 21",
            status=IssueStatus.WANTED,
        ),
    ]
    session.add_all(issues)
    await session.flush()
    return series, issues


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    series: Series | None = None,
    *,
    status: ImportSeriesStatus = ImportSeriesStatus.IMPORTED,
) -> ImportedSeries:
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        status=status,
        file_count=1,
        series_id=series.id if series is not None else None,
    )
    session.add(item)
    await session.flush()
    return item


def _make_file(
    job: ImportJob,
    item: ImportedSeries,
    *,
    status: ImportedFileStatus,
    issue_no: int,
    include_in_import: bool = False,
    is_preferred: bool = False,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path=f"/tmp/comics/Absolute Wonder Woman {issue_no:03d}.cbz",
        file_name=f"Absolute Wonder Woman {issue_no:03d}.cbz",
        file_size=1024,
        file_format="cbz",
        parsed_series="Absolute Wonder Woman",
        parsed_issue_number=float(issue_no),
        status=status,
        include_in_import=include_in_import,
        is_preferred=is_preferred,
    )


async def test_load_importable_files_excludes_unresolved_preferred_conflicts_for_new_series(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    item = await _create_imported_series(db_session, job)
    matched = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=19)
    confirmed = _make_file(job, item, status=ImportedFileStatus.CONFIRMED, issue_no=20)
    preferred_conflict = _make_file(
        job,
        item,
        status=ImportedFileStatus.CONFLICT,
        issue_no=21,
        is_preferred=True,
    )
    rejected_conflict = _make_file(
        job,
        item,
        status=ImportedFileStatus.CONFLICT,
        issue_no=22,
        is_preferred=False,
    )
    skipped = _make_file(job, item, status=ImportedFileStatus.SKIPPED, issue_no=23)
    db_session.add_all([matched, confirmed, preferred_conflict, rejected_conflict, skipped])
    await db_session.flush()

    files = await load_importable_files(db_session, item)

    assert files == [matched, confirmed]


async def test_load_importable_files_duplicate_mode_requires_selected_files(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    item = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
    )
    selected = _make_file(
        job,
        item,
        status=ImportedFileStatus.MATCHED,
        issue_no=19,
        include_in_import=True,
    )
    unselected = _make_file(
        job,
        item,
        status=ImportedFileStatus.MATCHED,
        issue_no=20,
        include_in_import=False,
    )
    preferred_conflict = _make_file(
        job,
        item,
        status=ImportedFileStatus.CONFLICT,
        issue_no=21,
        include_in_import=True,
        is_preferred=True,
    )
    db_session.add_all([selected, unselected, preferred_conflict])
    await db_session.flush()

    files = await load_importable_files(db_session, item, duplicate_mode=True)

    assert files == [selected]


async def test_load_importable_files_excludes_safety_blocked_files(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    item = await _create_imported_series(db_session, job)
    blocked = _make_file(
        job,
        item,
        status=ImportedFileStatus.SAFETY_BLOCKED,
        issue_no=19,
        include_in_import=True,
    )
    matched = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=20)
    db_session.add_all([blocked, matched])
    await db_session.flush()

    files = await load_importable_files(db_session, item)

    assert files == [matched]


async def test_load_issue_lookup_for_series_indexes_by_cv_id_and_issue_number(
    db_session: AsyncSession,
) -> None:
    series, issues = await _create_series_with_issues(db_session)

    cv_id_to_issue, exact_number_to_issue, number_to_issue = await load_issue_lookup_for_series(
        db_session,
        series.id,
    )

    assert cv_id_to_issue == {
        100019: issues[0],
        100020: issues[1],
        100021: issues[2],
    }
    assert number_to_issue == {
        19.0: issues[0],
        20.0: issues[1],
        21.0: issues[2],
    }
    assert exact_number_to_issue == {
        "19": issues[0],
        "20": issues[1],
        "21": issues[2],
    }


async def test_resolve_import_file_issue_uses_existing_issue_id_first(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    series, issues = await _create_series_with_issues(db_session)
    item = await _create_imported_series(db_session, job, series)
    imp_file = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=19)
    imp_file.matched_issue_id = issues[0].id
    imp_file.matched_issue_cv_id = issues[1].comicvine_id
    await db_session.flush()
    cv_id_to_issue, exact_number_to_issue, number_to_issue = await load_issue_lookup_for_series(
        db_session, series.id
    )

    resolved = await resolve_import_file_issue(
        db_session,
        imp_file,
        cv_id_to_issue=cv_id_to_issue,
        exact_number_to_issue=exact_number_to_issue,
        number_to_issue=number_to_issue,
    )

    assert resolved == issues[0]


async def test_resolve_import_file_issue_falls_back_through_cv_and_number(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    series, issues = await _create_series_with_issues(db_session)
    item = await _create_imported_series(db_session, job, series)
    matched_cv_file = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=19)
    matched_cv_file.matched_issue_cv_id = issues[1].comicvine_id
    comicinfo_cv_file = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=20)
    comicinfo_cv_file.comicvine_issue_id = issues[2].comicvine_id
    number_file = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=19)
    await db_session.flush()
    cv_id_to_issue, exact_number_to_issue, number_to_issue = await load_issue_lookup_for_series(
        db_session, series.id
    )

    assert (
        await resolve_import_file_issue(
            db_session,
            matched_cv_file,
            cv_id_to_issue=cv_id_to_issue,
            exact_number_to_issue=exact_number_to_issue,
            number_to_issue=number_to_issue,
        )
        == issues[1]
    )
    assert (
        await resolve_import_file_issue(
            db_session,
            comicinfo_cv_file,
            cv_id_to_issue=cv_id_to_issue,
            exact_number_to_issue=exact_number_to_issue,
            number_to_issue=number_to_issue,
        )
        == issues[2]
    )
    assert (
        await resolve_import_file_issue(
            db_session,
            number_file,
            cv_id_to_issue=cv_id_to_issue,
            exact_number_to_issue=exact_number_to_issue,
            number_to_issue=number_to_issue,
        )
        == issues[0]
    )


async def test_resolve_import_file_issue_returns_none_when_no_match(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    series, _issues = await _create_series_with_issues(db_session)
    item = await _create_imported_series(db_session, job, series)
    imp_file = _make_file(job, item, status=ImportedFileStatus.MATCHED, issue_no=99)
    cv_id_to_issue, exact_number_to_issue, number_to_issue = await load_issue_lookup_for_series(
        db_session, series.id
    )

    assert (
        await resolve_import_file_issue(
            db_session,
            imp_file,
            cv_id_to_issue=cv_id_to_issue,
            exact_number_to_issue=exact_number_to_issue,
            number_to_issue=number_to_issue,
        )
        is None
    )
