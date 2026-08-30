"""Tests for import review query helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from pullbox.core.exceptions import NotFoundError
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_review_queries import (
    MAX_CONFLICT_GROUP_FILES,
    get_conflict_groups,
    get_conflict_groups_page,
    get_files_for_series,
)
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_series_row(
    session: AsyncSession,
    job: ImportJob,
    *,
    name: str = "Absolute Wonder Woman",
    status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    diagnostics: dict[str, object] | None = None,
) -> ImportedSeries:
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=name,
        status=status,
        file_count=0,
        diagnostics=diagnostics or {},
    )
    session.add(series)
    await session.flush()
    return series


def _make_file(
    job: ImportJob,
    series: ImportedSeries,
    *,
    name: str,
    status: ImportedFileStatus = ImportedFileStatus.MATCHED,
    conflict_group_id: int | None = None,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/tmp/comics/{name}",
        file_name=name,
        file_size=1024,
        file_format="cbz",
        status=status,
        conflict_group_id=conflict_group_id,
    )


async def test_get_files_for_series_filters_and_paginates(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    series = await _create_series_row(db_session, job)
    db_session.add_all(
        [
            _make_file(job, series, name="issue-001.cbz", status=ImportedFileStatus.MATCHED),
            _make_file(job, series, name="issue-002.cbz", status=ImportedFileStatus.MATCHED),
            _make_file(job, series, name="issue-003.cbz", status=ImportedFileStatus.NO_MATCH),
        ]
    )
    await db_session.flush()

    files, total = await get_files_for_series(
        db_session,
        job.id,
        series.id,
        status_filter=ImportedFileStatus.MATCHED,
        page=2,
        page_size=1,
    )

    assert total == 2
    assert [file.file_name for file in files] == ["issue-002.cbz"]


async def test_get_files_for_series_requires_job_and_series(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)

    with pytest.raises(NotFoundError):
        await get_files_for_series(db_session, job.id + 999, 1)

    with pytest.raises(NotFoundError):
        await get_files_for_series(db_session, job.id, 1)


async def test_get_conflict_groups_sorts_series_conflicts_before_file_conflicts(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    file_series = await _create_series_row(db_session, job, name="File Conflict")
    series_conflict = await _create_series_row(
        db_session,
        job,
        name="Series Conflict",
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={"kind": "series_conflict", "reason": "ambiguous_match"},
    )
    db_session.add_all(
        [
            _make_file(
                job,
                file_series,
                name="variant-a.cbz",
                status=ImportedFileStatus.CONFLICT,
                conflict_group_id=7,
            ),
            _make_file(
                job,
                file_series,
                name="variant-b.cbz",
                status=ImportedFileStatus.CONFLICT,
                conflict_group_id=7,
            ),
            _make_file(job, series_conflict, name="series-conflict.cbz"),
        ]
    )
    await db_session.flush()

    groups = await get_conflict_groups(db_session, job.id)

    assert [group["kind"] for group in groups] == ["series_conflict", "file_conflict"]
    assert groups[0]["series_id"] == series_conflict.id
    assert groups[1]["conflict_group_id"] == 7
    assert [file.file_name for file in groups[1]["files"]] == [
        "variant-a.cbz",
        "variant-b.cbz",
    ]


async def test_get_conflict_groups_page_is_bounded_stable_and_query_constant(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    job = await _create_job_row(db_session)
    series_conflicts: list[ImportedSeries] = []
    file_conflict_series = await _create_series_row(
        db_session,
        job,
        name="File Conflicts",
    )
    for index in range(1, 53):
        series_conflict = await _create_series_row(
            db_session,
            job,
            name=f"Series Conflict {index}",
            status=ImportSeriesStatus.NO_MATCH,
            diagnostics={"kind": "series_conflict", "reason": "ambiguous_match"},
        )
        series_conflicts.append(series_conflict)
        db_session.add(
            _make_file(
                job,
                series_conflict,
                name=f"series-conflict-{index:03}.cbz",
            )
        )
        db_session.add_all(
            [
                _make_file(
                    job,
                    file_conflict_series,
                    name=f"variant-{index:03}-a.cbz",
                    status=ImportedFileStatus.CONFLICT,
                    conflict_group_id=index,
                ),
                _make_file(
                    job,
                    file_conflict_series,
                    name=f"variant-{index:03}-b.cbz",
                    status=ImportedFileStatus.CONFLICT,
                    conflict_group_id=index,
                ),
            ]
        )
    await db_session.flush()

    selects: list[str] = []

    def record_select(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    observed_group_ids: list[object] = []
    observed_totals: list[int] = []
    compatibility_group_ids: list[object] = []
    event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
    try:
        for page_number in range(1, 6):
            selects.clear()
            page = await get_conflict_groups_page(
                db_session,
                job.id,
                page=page_number,
                page_size=25,
            )
            observed_totals.append(page.total)
            observed_group_ids.extend(group["conflict_group_id"] for group in page.items)
            assert len(page.items) <= 25
            assert len(selects) <= 5, (
                f"conflict page {page_number} issued {len(selects)} SELECT statements"
            )

        selects.clear()
        compatibility_groups = await get_conflict_groups(db_session, job.id)
        compatibility_group_ids = [group["conflict_group_id"] for group in compatibility_groups]
        assert len(selects) <= 5, (
            f"compatibility conflict query issued {len(selects)} SELECT statements"
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

    expected_series_ids = [
        f"series-{series.id}" for series in sorted(series_conflicts, key=lambda item: str(item.id))
    ]
    expected_file_group_ids = sorted(range(1, 53), key=str)
    expected_group_ids = [*expected_series_ids, *expected_file_group_ids]
    assert observed_totals == [104] * 5
    assert observed_group_ids == expected_group_ids
    assert compatibility_group_ids == expected_group_ids


async def test_conflict_group_pages_preserve_global_sort_counts_and_file_bounds(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    expected: list[dict[str, object]] = []

    for index in range(30):
        series = await _create_series_row(
            db_session,
            job,
            name=f"The Series {29 - index:02d}",
        )
        series.raw_year = 2020 + (index % 3)
        file_count = (index % 4) + 1
        preferred = index % 2 == 0
        issue_number = float(index % 7)
        for file_index in range(file_count):
            imp_file = _make_file(
                job,
                series,
                name=f"variant-{index:02d}-{file_index:02d}.cbz",
                status=ImportedFileStatus.CONFLICT,
                conflict_group_id=100 + index,
            )
            imp_file.parsed_issue_number = issue_number
            imp_file.is_preferred = preferred and file_index == 0
            db_session.add(imp_file)
        expected.append(
            {
                "conflict_group_id": 100 + index,
                "kind": "file_conflict",
                "series": f"{series.raw_series_name} ({series.raw_year})",
                "issue": issue_number,
                "files": file_count,
                "signal": "auto-selected" if preferred else "needs choice",
                "status": "auto-selected" if preferred else "needs choice",
            }
        )

    series_conflict = await _create_series_row(
        db_session,
        job,
        name="Aardvark Annual",
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={
            "kind": "series_conflict",
            "selected_candidate": {"title": "Zebra Candidate"},
        },
    )
    for file_index in range(MAX_CONFLICT_GROUP_FILES + 25):
        db_session.add(
            _make_file(
                job,
                series_conflict,
                name=f"annual-{file_index:03d}.cbz",
            )
        )
    await db_session.flush()
    expected.append(
        {
            "conflict_group_id": f"series-{series_conflict.id}",
            "kind": "series_conflict",
            "series": "Aardvark Annual",
            "issue": None,
            "files": MAX_CONFLICT_GROUP_FILES + 25,
            "signal": "Zebra Candidate",
            "status": "series match conflict",
        }
    )

    def default_key(item: dict[str, object]) -> tuple[object, ...]:
        issue = item["issue"]
        issue_key = (1, 0.0, "") if issue is None else (0, float(issue), str(issue))
        return (
            NameMatcher.normalize(str(item["series"])),
            0 if item["kind"] == "series_conflict" else 1,
            issue_key,
            str(item["conflict_group_id"]),
        )

    def sort_key(item: dict[str, object], field: str) -> tuple[object, ...]:
        if field == "conflict":
            issue = item["issue"]
            issue_key = (1, 0.0, "") if issue is None else (0, float(issue), str(issue))
            return (
                0 if item["kind"] == "series_conflict" else 1,
                issue_key,
                default_key(item),
            )
        if field == "files":
            return (int(item["files"]), default_key(item))
        if field in {"signal", "status"}:
            return (NameMatcher.normalize(str(item[field])), default_key(item))
        return default_key(item)

    for sort in ("series", "-conflict", "files", "-signal", "status"):
        observed: list[object] = []
        for page_number in range(1, 6):
            page = await get_conflict_groups_page(
                db_session,
                job.id,
                page=page_number,
                page_size=7,
                sort=sort,
            )
            observed.extend(group["conflict_group_id"] for group in page.items)
            assert page.total == 31
            assert page.auto_resolved == 15
            assert page.needs_decision == 15
            assert page.series_candidate_conflicts == 1
            assert page.file_conflict_groups == 30

        field = sort.removeprefix("-")
        ordered = sorted(
            expected,
            key=lambda item: sort_key(item, field),
            reverse=sort.startswith("-"),
        )
        assert observed == [item["conflict_group_id"] for item in ordered]

    clamped = await get_conflict_groups_page(
        db_session,
        job.id,
        page=999,
        page_size=25,
        sort="series",
    )
    assert clamped.page == 2

    annual_page = await get_conflict_groups_page(
        db_session,
        job.id,
        page=1,
        page_size=1,
        sort="series",
    )
    annual_group = annual_page.items[0]
    assert annual_group["conflict_group_id"] == f"series-{series_conflict.id}"
    assert annual_group["file_count"] == MAX_CONFLICT_GROUP_FILES + 25
    assert len(annual_group["files"]) == MAX_CONFLICT_GROUP_FILES
    assert annual_group["files_truncated"] is True


async def test_import_service_review_query_shims_remain_available(
    db_session: AsyncSession,
) -> None:
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )
    job = await _create_job_row(db_session)
    series = await _create_series_row(db_session, job)

    files, total = await service.get_files_for_series(db_session, job.id, series.id)
    groups = await service.get_conflict_groups(db_session, job.id)

    assert files == []
    assert total == 0
    assert groups == []
