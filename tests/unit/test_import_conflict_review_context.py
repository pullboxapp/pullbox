"""Tests for import Step 3 conflict-review context assembly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series, SeriesStatus
from pullbox.services.import_review_queries import ConflictGroupsPage
from pullbox.ui import import_conflict_review

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeConflictService:
    def __init__(self, groups: list[dict[str, object]]) -> None:
        self.groups = groups
        self.page_calls: list[tuple[int, int, str]] = []

    async def get_conflict_groups_page(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        page: int,
        page_size: int,
        sort: str,
    ) -> ConflictGroupsPage:
        _ = (session, job_id)
        self.page_calls.append((page, page_size, sort))
        total = len(self.groups)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(page, total_pages)
        start = (current_page - 1) * page_size
        items: list[dict[str, object]] = []
        for original in self.groups[start : start + page_size]:
            group = dict(original)
            files = [item for item in group.get("files", []) if isinstance(item, ImportedFile)]
            group.setdefault("file_count", len(files))
            group.setdefault("files_truncated", False)
            if group.get("series_id") is None and files:
                group["series_id"] = files[0].import_series_id
            items.append(group)
        file_groups = [group for group in self.groups if group.get("kind") == "file_conflict"]
        auto_resolved = sum(
            1
            for group in file_groups
            if any(
                item.is_preferred
                for item in group.get("files", [])
                if isinstance(item, ImportedFile)
            )
        )
        return ConflictGroupsPage(
            items=tuple(items),
            total=total,
            page=current_page,
            page_size=page_size,
            auto_resolved=auto_resolved,
            needs_decision=len(file_groups) - auto_resolved,
            series_candidate_conflicts=sum(
                1 for group in self.groups if group.get("kind") == "series_conflict"
            ),
            file_conflict_groups=len(file_groups),
        )


async def _create_import_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/imports/current",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    raw_series_name: str = "Absolute Batman",
    raw_year: int | None = 2024,
    source_folder: str | None = "/imports/absolute-batman",
    diagnostics: dict[str, object] | None = None,
) -> ImportedSeries:
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=raw_series_name,
        raw_year=raw_year,
        source_folder=source_folder,
        file_count=1,
        files_total=1,
        status=ImportSeriesStatus.MATCHED,
        diagnostics=diagnostics or {},
    )
    session.add(imported_series)
    await session.flush()
    return imported_series


def _make_imported_file(
    job: ImportJob,
    imported_series: ImportedSeries,
    *,
    file_name: str = "Absolute Batman 001 (2024).cbz",
    parsed_series: str | None = "Absolute Batman",
    parsed_issue_number: float | None = 1.0,
    parsed_year: int | None = 2024,
    status: ImportedFileStatus = ImportedFileStatus.CONFLICT,
    diagnostics: dict[str, object] | None = None,
    is_preferred: bool = False,
    has_comicinfo: bool = False,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path=f"/imports/current/{file_name}",
        file_name=file_name,
        file_size=1024,
        file_format=file_name.rsplit(".", 1)[-1],
        parsed_series=parsed_series,
        parsed_issue_number=parsed_issue_number,
        parsed_year=parsed_year,
        has_comicinfo=has_comicinfo,
        status=status,
        is_preferred=is_preferred,
        match_method="test_match",
        diagnostics=diagnostics or {},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, 1),
        (False, 0),
        (7, 7),
        (7.9, 7),
        ("12", 12),
        ("not-a-number", -1),
        (object(), -1),
    ],
)
def test_object_to_int_coerces_supported_values(value: object, expected: int) -> None:
    assert import_conflict_review._object_to_int(value, default=-1) == expected


@pytest.mark.asyncio
async def test_load_import_conflict_review_context_rejects_missing_job(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(NotFoundError):
        await import_conflict_review._load_import_conflict_review_context(
            999,
            db_session,
        )


@pytest.mark.asyncio
async def test_load_import_conflict_review_context_enriches_file_conflicts(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _create_import_job(db_session)
    imported_series = await _create_imported_series(
        db_session,
        job,
        raw_series_name="Drifter",
        raw_year=2025,
        source_folder="/imports/drifter",
    )
    library_series = Series(
        title="Drifter",
        sort_title="drifter",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
    )
    db_session.add(library_series)
    await db_session.flush()
    issue = Issue(series_id=library_series.id, issue_number=2.0, title="Night Moves")
    db_session.add(issue)
    await db_session.flush()

    preferred = _make_imported_file(
        job,
        imported_series,
        file_name="Drifter 002 (2025).cbz",
        parsed_series="Drifter",
        parsed_issue_number=2.0,
        diagnostics={"reason": "best quality"},
        is_preferred=True,
    )
    alternate = _make_imported_file(
        job,
        imported_series,
        file_name="Drifter Variant 002 (2025).cbz",
        parsed_series="Drifter Variant",
        parsed_issue_number=2.0,
    )
    db_session.add_all([preferred, alternate])
    await db_session.flush()

    monkeypatch.setattr(
        import_conflict_review,
        "build_import_control_service",
        lambda: _FakeConflictService(
            [
                {
                    "kind": "file_conflict",
                    "conflict_group_id": "file-2",
                    "matched_issue_id": issue.id,
                    "files": [preferred, alternate],
                }
            ]
        ),
    )

    context = await import_conflict_review._load_import_conflict_review_context(
        job.id,
        db_session,
        sort="status",
    )

    assert context["job"] is job
    assert context["auto_resolved"] == 1
    assert context["needs_decision"] == 0
    assert context["file_conflict_group_count"] == 1
    assert context["visible_file_conflict_group_count"] == 1

    group = context["conflict_groups"][0]
    assert group["kind"] == "file_conflict"
    assert group["issue"] is issue
    assert group["series_name"] == "Drifter (2025)"
    assert group["source_folder"] == "/imports/drifter"
    assert group["display_issue_number"] == 2.0
    assert group["has_preferred"] is True
    assert group["file_count"] == 2
    assert group["parsed_series_names"] == ["Drifter", "Drifter Variant"]
    assert group["mixed_series_bucket"] is True
    assert group["diagnostics"] == {"reason": "best quality"}


@pytest.mark.asyncio
async def test_load_import_conflict_review_context_enriches_series_conflicts(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _create_import_job(db_session)
    imported_series = await _create_imported_series(
        db_session,
        job,
        raw_series_name="Absolute Batman",
        raw_year=2024,
        source_folder="/imports/absolute-batman",
    )
    current_file = _make_imported_file(
        job,
        imported_series,
        diagnostics={
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Absolute Batman",
                    "issue_number": 1.0,
                    "year": 2024,
                },
                "comicinfo": {"series": "Absolute Batman", "number": "1"},
            },
            "metadata_signals": {"web_cv_id": 162966},
        },
        has_comicinfo=True,
    )
    sibling_series = await _create_imported_series(
        db_session,
        job,
        raw_series_name="Absolute Batman",
        raw_year=2025,
        source_folder="/imports/absolute-batman-2025",
    )
    related_file = _make_imported_file(
        job,
        sibling_series,
        file_name="Absolute Batman 002 (2025).cbz",
        parsed_year=2025,
        diagnostics={
            "source_metadata": {
                "filename_parse": {"series_name": "Absolute Batman", "year": 2025},
                "comicinfo": {"series": "Absolute Batman", "number": "2"},
            }
        },
        has_comicinfo=True,
    )
    db_session.add_all([current_file, related_file])
    await db_session.flush()

    monkeypatch.setattr(
        import_conflict_review,
        "build_import_control_service",
        lambda: _FakeConflictService(
            [
                {
                    "kind": "series_conflict",
                    "conflict_group_id": "series-1",
                    "series_id": imported_series.id,
                    "files": [current_file],
                    "diagnostics": {"selected_candidate": {"title": "Absolute Batman"}},
                }
            ]
        ),
    )

    context = await import_conflict_review._load_import_conflict_review_context(
        job.id,
        db_session,
        sort="-signal",
    )

    assert context["series_candidate_conflicts"] == 1
    group = context["conflict_groups"][0]
    assert group["kind"] == "series_conflict"
    assert group["series_id"] == imported_series.id
    assert group["series_name"] == "Absolute Batman (2024)"
    assert group["raw_series_name"] == "Absolute Batman"
    assert group["source_folder"] == "/imports/absolute-batman"
    assert group["file_count"] == 1
    assert group["parsed_series_names"] == ["Absolute Batman"]
    assert group["diagnostics"] == {"selected_candidate": {"title": "Absolute Batman"}}

    source_file = group["source_files"][0]
    assert source_file["file_id"] == current_file.id
    assert source_file["filename_series"] == "Absolute Batman"
    assert source_file["filename_issue_number"] == 1.0
    assert source_file["comicinfo"] == {"series": "Absolute Batman", "number": "1"}
    assert source_file["metadata_signals"] == {"web_cv_id": 162966}
    assert source_file["current_series_label"] == "Absolute Batman (2024)"

    related_file_summary = group["related_source_files"][0]
    assert related_file_summary["file_id"] == related_file.id
    assert related_file_summary["comicinfo"] == {"series": "Absolute Batman", "number": "2"}
    assert related_file_summary["current_series_label"] == "Absolute Batman (2025)"


@pytest.mark.asyncio
async def test_series_conflict_context_extracts_comicinfo_from_archive_when_needed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _create_import_job(db_session)
    imported_series = await _create_imported_series(db_session, job)
    imported_file = _make_imported_file(
        job,
        imported_series,
        file_name="Archive Only 001.cbz",
        parsed_series=None,
        parsed_issue_number=None,
        parsed_year=None,
        has_comicinfo=True,
    )
    db_session.add(imported_file)
    await db_session.flush()

    class _Extractor:
        def from_archive_path(self, path: str) -> SimpleNamespace:
            return SimpleNamespace(diagnostics={"comicinfo": {"series": path, "number": "1"}})

    monkeypatch.setattr(import_conflict_review, "SourceMetadataExtractor", _Extractor)
    monkeypatch.setattr(
        import_conflict_review,
        "build_import_control_service",
        lambda: _FakeConflictService(
            [
                {
                    "kind": "series_conflict",
                    "conflict_group_id": "series-archive",
                    "series_id": imported_series.id,
                    "files": [imported_file],
                }
            ]
        ),
    )

    context = await import_conflict_review._load_import_conflict_review_context(
        job.id,
        db_session,
    )

    source_file = context["conflict_groups"][0]["source_files"][0]
    assert source_file["comicinfo"] == {"series": imported_file.file_path, "number": "1"}
    assert source_file["filename_series"] == "Archive Only"
    assert source_file["filename_issue_number"] == 1.0


@pytest.mark.asyncio
async def test_load_import_conflict_review_context_normalizes_sort_and_paginates(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _create_import_job(db_session)
    groups = [
        {
            "kind": "file_conflict",
            "conflict_group_id": f"group-{index:02d}",
            "matched_issue_id": None,
            "files": [],
        }
        for index in range(30)
    ]
    monkeypatch.setattr(
        import_conflict_review,
        "build_import_control_service",
        lambda: _FakeConflictService(groups),
    )

    context = await import_conflict_review._load_import_conflict_review_context(
        job.id,
        db_session,
        page=99,
        sort="unknown",
    )

    assert context["sort"] == "series"
    assert context["total_groups"] == 30
    assert context["total_pages"] == 2
    assert context["page"] == 2
    assert context["page_size"] == 25
    assert len(context["conflict_groups"]) == 5
    assert context["needs_decision"] == 30
    assert context["auto_resolved"] == 0


@pytest.mark.asyncio
async def test_conflict_review_context_keeps_page_enrichment_query_constant(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    job = await _create_import_job(db_session)
    for index in range(75):
        imported_series = await _create_imported_series(
            db_session,
            job,
            raw_series_name=f"Paged Conflict {index:03d}",
            diagnostics={
                "kind": "series_conflict",
                "selected_candidate": {"title": f"Candidate {index:03d}"},
            },
        )
        imported_series.status = ImportSeriesStatus.NO_MATCH
        db_session.add(
            _make_imported_file(
                job,
                imported_series,
                file_name=f"Paged Conflict {index:03d} 001.cbz",
                status=ImportedFileStatus.NO_MATCH,
            )
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
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
    try:
        context = await import_conflict_review._load_import_conflict_review_context(
            job.id,
            db_session,
            page=2,
            sort="series",
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

    assert context["total_groups"] == 75
    assert context["series_candidate_conflicts"] == 75
    assert context["page"] == 2
    assert len(context["conflict_groups"]) == 25
    assert len(selects) <= 7, f"paged conflict enrichment issued {len(selects)} SELECTs"
