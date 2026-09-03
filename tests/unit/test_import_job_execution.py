"""Runtime discipline tests for Step 4 import execution helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullbox.services.import_job_execution as import_job_execution
from pullbox.core.exceptions import JobCancelledError
from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services import library_root_management
from pullbox.services.import_file_execution import process_import_series_files
from pullbox.services.import_job_execution import (
    _active_file_progress_pct,
    _ActiveFileProgressSettings,
    _build_execution_item_plans,
    _emit_import_preparation_progress,
    _execute_duplicate_series_merge,
    _execute_new_series,
    _prime_series_prefetch_window,
    _targeted_issue_summaries_for_import_files,
    execute_import_job,
)
from pullbox.services.import_job_execution_types import ExecutionItemPlan
from pullbox.services.import_workflow_state import emit_progress


@pytest.mark.parametrize("source_type", [ImportSourceType.MYLAR3, ImportSourceType.FILESYSTEM])
async def test_completed_group_eta_uses_remaining_work_before_one_percent(
    db_session, monkeypatch, source_type
):
    from pullbox.services import import_progress_runtime
    from pullbox.services.import_operation_progress import build_import_operation_update
    from pullbox.services.import_workflow_state import estimate_remaining_seconds
    from pullbox.ui.import_progress_snapshot import build_import_progress_snapshot

    job = ImportJob(
        source_path="/tmp/imports",
        source_type=source_type,
        import_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add_all(
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Series {index:04d}",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=900_000 + index,
        )
        for index in range(300)
    )
    await db_session.commit()
    events = []
    measured_starts = []
    invocation_start = datetime.now(UTC)

    def elapsed(started_at):
        measured_starts.append(started_at)
        return 20

    async def complete_group(_session, _job, item, **kwargs):
        item.status = ImportSeriesStatus.IMPORTED
        return 0, 0, kwargs["imported_count"] + 1, 0, True

    async def capture(event):
        events.append(event.model_copy(deep=True))

    async def stop_after_group():
        raise JobCancelledError()

    monkeypatch.setattr(import_progress_runtime, "elapsed_seconds_since", elapsed)
    monkeypatch.setattr(import_job_execution, "_execute_new_series", complete_group)
    with pytest.raises(JobCancelledError):
        await execute_import_job(
            db_session,
            job.id,
            series_service=object(),
            process_series_files=AsyncMock(),
            raise_if_cancelled=AsyncMock(),
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            emit_progress=emit_progress,
            estimate_remaining_seconds=estimate_remaining_seconds,
            maybe_slow_item_delay=stop_after_group,
            progress_callback=capture,
        )

    completed = next(event for event in events if event.message == "Processed 1/300 review groups")
    assert completed.progress == 0
    assert completed.estimated_seconds_remaining == 5980
    assert measured_starts and all(start >= invocation_start for start in measured_starts)
    await db_session.refresh(job)
    hydrated = build_import_progress_snapshot(
        job, review_summary={}, recent_logs=[], progress_revision=job.progress_revision
    )
    assert hydrated["estimated_seconds_remaining"] == 5980
    assert build_import_operation_update(job, completed).eta_seconds == 5980


@pytest.fixture(autouse=True)
async def default_managed_root(
    db_session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> LibraryRoot:
    """Give direct execution fixtures the configured managed-root invariant."""
    root_path = tmp_path / "execution-managed-root"
    root_path.mkdir()
    root = LibraryRoot(
        name="Execution managed root",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    db_session.add(root)
    await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    return root


async def _add_matched_file(
    db_session,
    job: ImportJob,
    item: ImportedSeries,
    *,
    file_name: str = "Issue 001.cbz",
) -> None:
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path=f"/tmp/imports/{file_name}",
            file_name=file_name,
            file_size=1024,
            file_format=file_name.rsplit(".", 1)[-1],
            parsed_issue_number=1.0,
            status=ImportedFileStatus.MATCHED,
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_execute_import_job_reopens_review_when_managed_capacity_drifted(
    db_session,
    default_managed_root: LibraryRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
        target_library_root_id=default_managed_root.id,
        progress_snapshot={
            "managed_copy_capacity": {
                "schema_version": 1,
                "stage": "confirmation",
                "target_library_root_id": default_managed_root.id,
                "selected_source_bytes": 20 * 1024**3,
                "reserve_bytes": 2 * 1024**3,
                "required_bytes": 22 * 1024**3,
                "free_bytes": 30 * 1024**3,
                "status": "ready",
            }
        },
    )
    db_session.add(job)
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Capacity Drift",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=999_101,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path="/tmp/imports/Capacity Drift 001.cbz",
        file_name="Capacity Drift 001.cbz",
        file_size=20 * 1024**3,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
    )
    db_session.add(imp_file)
    await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=22 * 1024**3 - 1),
    )
    series_service = AsyncMock()
    process_series_files = AsyncMock()

    await execute_import_job(
        db_session,
        job.id,
        series_service=series_service,
        process_series_files=process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
    )

    await db_session.refresh(job)
    await db_session.refresh(item)
    await db_session.refresh(imp_file)
    assert job.status == ImportJobStatus.REVIEW
    assert job.import_started_at is None
    assert job.progress_snapshot["managed_copy_capacity"]["stage"] == "execution"
    assert job.progress_snapshot["managed_copy_capacity"]["status"] == "insufficient"
    assert item.status == ImportSeriesStatus.MATCHED
    assert imp_file.status == ImportedFileStatus.MATCHED
    series_service.assert_not_awaited()
    process_series_files.assert_not_awaited()


def test_build_execution_item_plans_preserves_review_group_modes_and_cv_precedence() -> None:
    confirmed = ImportedSeries(
        id=11,
        import_job_id=1,
        raw_series_name="King Dracula",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=100,
        user_selected_cv_id=200,
        series_id=None,
    )
    duplicate = ImportedSeries(
        id=12,
        import_job_id=1,
        raw_series_name="Existing Series",
        status=ImportSeriesStatus.DUPLICATE,
        cv_id=300,
        user_selected_cv_id=None,
        series_id=44,
    )

    plans = _build_execution_item_plans([confirmed], [duplicate])

    assert [plan.mode for plan in plans] == ["new", "duplicate"]
    assert plans[0].item_id == 11
    assert plans[0].raw_series_name == "King Dracula"
    assert plans[0].cv_id == 200
    assert plans[0].existing_series_id is None
    assert plans[1].item_id == 12
    assert plans[1].raw_series_name == "Existing Series"
    assert plans[1].cv_id == 300
    assert plans[1].existing_series_id == 44


@pytest.mark.asyncio
async def test_duplicate_in_place_import_sets_only_future_preferred_root(
    db_session,
    default_managed_root: LibraryRoot,
    tmp_path,
) -> None:
    future_path = tmp_path / "future-root"
    future_path.mkdir()
    future_root = LibraryRoot(
        name="Future root",
        path=str(future_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
    )
    db_session.add(future_root)
    await db_session.flush()
    existing_path = str(tmp_path / "existing-series")
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2011,
        comicvine_id=796,
        path=existing_path,
        library_root_id=default_managed_root.id,
        preferred_library_root_id=None,
    )
    db_session.add(series)
    await db_session.flush()
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        target_library_root_id=future_root.id,
    )
    db_session.add(job)
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        status=ImportSeriesStatus.DUPLICATE,
        cv_id=796,
        series_id=series.id,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    record_action = AsyncMock()

    result = await _execute_duplicate_series_merge(
        db_session,
        job,
        item,
        process_series_files=AsyncMock(return_value=(1, 0)),
        record_action=record_action,
        log_event=AsyncMock(),
    )

    await db_session.refresh(series)
    assert result == (1, 0, True)
    assert series.preferred_library_root_id == future_root.id
    assert series.library_root_id == default_managed_root.id
    assert series.path == existing_path
    record_action.assert_awaited_once_with(
        db_session,
        job,
        phase="import",
        action_type="series_preferred_root_updated",
        payload={
            "series_id": series.id,
            "old_preferred_library_root_id": None,
            "new_preferred_library_root_id": future_root.id,
        },
    )


@pytest.mark.asyncio
async def test_emit_import_preparation_progress_advances_revision_and_uses_job_counts() -> None:
    calls: list[dict[str, object]] = []
    job = ImportJob(
        id=9,
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        series_found=5,
        series_imported=1,
        series_failed=2,
        total_files_imported=3,
        total_files_failed=4,
        progress_revision=7,
    )
    revision_state = {"value": 7}

    async def emit_progress_stub(
        session: object,
        active_job: ImportJob,
        event: ImportProgressEvent,
        callback: object,
    ) -> None:
        calls.append(
            {
                "session": session,
                "job": active_job,
                "event": event,
                "callback": callback,
            }
        )

    async def callback(_event: ImportProgressEvent) -> None:
        return None

    session = object()

    await _emit_import_preparation_progress(
        session,
        job,
        job_id=9,
        progress_callback=callback,
        emit_progress=emit_progress_stub,
        runtime_revision_state=revision_state,
    )

    assert revision_state["value"] == 8
    assert job.progress_revision == 8
    assert len(calls) == 1
    event = calls[0]["event"]
    assert isinstance(event, ImportProgressEvent)
    assert event.job_id == 9
    assert event.status == ImportJobStatus.IMPORTING
    assert event.mode == "import"
    assert event.phase == "queued"
    assert event.progress == 0
    assert event.message == "Preparing the selected series for import..."
    assert event.progress_revision == 8
    assert event.series_found == 5
    assert event.series_imported == 1
    assert event.series_failed == 2
    assert event.total_files_imported == 3
    assert event.total_files_failed == 4


@pytest.mark.asyncio
async def test_prime_series_prefetch_window_skips_targeted_import_services() -> None:
    class TargetedSeriesService:
        async def add_from_import_review_targeted(self) -> None:
            return None

        async def prefetch_comicvine_bundle(
            self, _comicvine_id: int
        ) -> tuple[object, list[object]]:
            raise AssertionError("targeted imports should not prefetch full bundles")

    prefetch_tasks: dict[int, asyncio.Task[tuple[object, list[object]]]] = {}

    primed = _prime_series_prefetch_window(
        series_service=TargetedSeriesService(),
        execution_items=[
            ExecutionItemPlan(
                mode="new",
                item_id=1,
                raw_series_name="Large Series",
                cv_id=100,
                existing_series_id=None,
            )
        ],
        prefetch_tasks=prefetch_tasks,
        start_index=0,
        window_size=2,
    )

    assert primed == 0
    assert prefetch_tasks == {}


@pytest.mark.asyncio
async def test_prime_series_prefetch_window_primes_new_cv_groups_only() -> None:
    calls: list[int] = []

    class PrefetchSeriesService:
        async def prefetch_comicvine_bundle(self, comicvine_id: int) -> tuple[object, list[object]]:
            calls.append(comicvine_id)
            return ({"id": comicvine_id}, [])

    existing_task = asyncio.create_task(asyncio.sleep(0, result=({"id": 100}, [])))
    prefetch_tasks: dict[int, asyncio.Task[tuple[object, list[object]]]] = {1: existing_task}
    execution_items = [
        ExecutionItemPlan(
            mode="new",
            item_id=1,
            raw_series_name="Already Primed",
            cv_id=100,
            existing_series_id=None,
        ),
        ExecutionItemPlan(
            mode="duplicate",
            item_id=2,
            raw_series_name="Duplicate",
            cv_id=200,
            existing_series_id=99,
        ),
        ExecutionItemPlan(
            mode="new",
            item_id=3,
            raw_series_name="Missing CV",
            cv_id=None,
            existing_series_id=None,
        ),
        ExecutionItemPlan(
            mode="new",
            item_id=4,
            raw_series_name="First New",
            cv_id=400,
            existing_series_id=None,
        ),
        ExecutionItemPlan(
            mode="new",
            item_id=5,
            raw_series_name="Second New",
            cv_id=500,
            existing_series_id=None,
        ),
        ExecutionItemPlan(
            mode="new",
            item_id=6,
            raw_series_name="Outside Window",
            cv_id=600,
            existing_series_id=None,
        ),
    ]

    primed = _prime_series_prefetch_window(
        series_service=PrefetchSeriesService(),
        execution_items=execution_items,
        prefetch_tasks=prefetch_tasks,
        start_index=0,
        window_size=2,
    )

    assert primed == 2
    assert set(prefetch_tasks) == {1, 4, 5}
    assert await prefetch_tasks[4] == ({"id": 400}, [])
    assert await prefetch_tasks[5] == ({"id": 500}, [])
    assert calls == [400, 500]
    await existing_task


@pytest.mark.asyncio
async def test_execute_new_series_keeps_review_row_clean_during_external_work(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Persephone",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=110930,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Persephone 001.cbz")

    observations: dict[str, object] = {}

    async def _add_from_comicvine(_self, session, comicvine_id, *_args, **_kwargs):
        observations["dirty_during_series_fetch"] = bool(
            db_session.is_modified(item, include_collections=False) or item in db_session.dirty
        )
        created = Series(
            title="Persephone",
            sort_title="persephone",
            comicvine_id=comicvine_id,
        )
        session.add(created)
        await session.flush()
        return created

    async def _process_series_files(
        session,
        _job,
        _item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ):
        observations["dirty_during_file_processing"] = bool(
            session.is_modified(item, include_collections=False) or item in session.dirty
        )
        observations["series_id_override"] = series_id_override
        observations["duplicate_mode"] = duplicate_mode
        return (1, 0)

    record_action = AsyncMock()
    log_event = AsyncMock()

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=type("SeriesServiceStub", (), {"add_from_comicvine": _add_from_comicvine})(),
        process_series_files=_process_series_files,
        record_action=record_action,
        log_event=log_event,
    )

    assert files_ok == 1
    assert files_err == 0
    assert imported_count == 1
    assert failed_count == 0
    assert should_emit is True
    assert observations["dirty_during_series_fetch"] is False
    assert observations["dirty_during_file_processing"] is False
    assert isinstance(observations["series_id_override"], int)
    assert observations["series_id_override"] > 0
    assert observations["duplicate_mode"] is False
    assert item.status == ImportSeriesStatus.IMPORTED
    assert item.series_id == observations["series_id_override"]


@pytest.mark.asyncio
async def test_execute_new_series_uses_targeted_import_path_when_available(
    db_session,
    tmp_path,
) -> None:
    preferred_path = tmp_path / "preferred"
    preferred_path.mkdir()
    preferred_root = LibraryRoot(name="Preferred", path=str(preferred_path), enabled=True)
    db_session.add(preferred_root)
    await db_session.flush()
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        target_library_root_id=preferred_root.id,
        search_on_add=True,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="King Dracula",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=166904,
        cv_title="King Dracula",
        cv_year=2025,
        cv_publisher="Dynamite",
        cv_issue_count=3,
        cv_url="https://comicvine.gamespot.com/king-dracula/4050-166904/",
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path="/tmp/imports/King Dracula 004.cbz",
            file_name="King Dracula 004.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=4.0,
            status=ImportedFileStatus.MATCHED,
            diagnostics={
                "kind": "provider_missing_issue_placeholder",
                "target_issue_number": 4.0,
                "target_issue_type": "issue",
                "target_issue_title": None,
            },
        )
    )
    await db_session.flush()
    calls: dict[str, object] = {}

    class SeriesServiceStub:
        async def add_from_import_review_targeted(
            self,
            session,
            *,
            import_series: ImportedSeries,
            library_root_id: int | None,
            search_on_add: bool,
            issue_summaries: list[object],
        ) -> Series:
            calls["targeted"] = {
                "cv_id": import_series.cv_id,
                "library_root_id": library_root_id,
                "search_on_add": search_on_add,
                "issue_summaries": issue_summaries,
            }
            series = Series(
                title=import_series.cv_title or import_series.raw_series_name,
                sort_title="king dracula",
                year_start=import_series.cv_year,
                comicvine_id=import_series.cv_id,
                issue_catalog_state=IssueCatalogState.HYDRATING,
            )
            session.add(series)
            await session.flush()
            return series

        async def add_from_comicvine(self, *_args, **_kwargs) -> Series:
            raise AssertionError("full ComicVine add should not be used by targeted imports")

    async def _process_series_files(*_args, **kwargs) -> tuple[int, int]:
        calls["series_id_override"] = kwargs["series_id_override"]
        return (1, 0)

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        record_action=AsyncMock(),
        log_event=AsyncMock(),
    )

    assert files_ok == 1
    assert files_err == 0
    assert imported_count == 1
    assert failed_count == 0
    assert should_emit is True
    assert calls["targeted"] == {
        "cv_id": 166904,
        "library_root_id": None,
        "search_on_add": True,
        "issue_summaries": [],
    }
    assert calls["series_id_override"] == item.series_id
    assert item.status == ImportSeriesStatus.IMPORTED
    created_series = await db_session.get(Series, item.series_id)
    assert created_series is not None
    assert created_series.library_root_id is None
    assert created_series.preferred_library_root_id == preferred_root.id


@pytest.mark.asyncio
async def test_execute_existing_series_records_owned_local_cover_for_rollback(
    db_session,
    tmp_path,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    existing = Series(
        title="Existing",
        sort_title="existing",
        comicvine_id=166905,
        cover_path="https://example.invalid/prior.jpg",
    )
    db_session.add_all([job, existing])
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Existing",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=166905,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Existing 001.cbz")
    covers_base = tmp_path / ".covers"
    cover_dir = covers_base / str(existing.id)
    cover_dir.mkdir(parents=True)
    cover_path = cover_dir / "series.jpg"
    cover_path.write_bytes(b"import cover")

    class SeriesServiceStub:
        async def add_from_import_review_targeted(
            self,
            _session,
            *,
            import_series: ImportedSeries,
            **_kwargs,
        ) -> Series:
            import_series.diagnostics = {
                "cover_cache_ownership": {
                    "schema_version": 1,
                    "base_path": str(covers_base),
                    "ownership_boundary_path": str(tmp_path),
                    "created_directory_paths": [str(covers_base), str(cover_dir)],
                    "artifact_path": str(cover_path),
                    "artifact_signature": build_managed_placement_signature(cover_path),
                }
            }
            existing.cover_path = f"/api/v1/series/{existing.id}/cover"
            return existing

    record_action = AsyncMock()
    await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=AsyncMock(return_value=(1, 0)),
        record_action=record_action,
        log_event=AsyncMock(),
    )

    cover_calls = [
        call
        for call in record_action.await_args_list
        if call.kwargs.get("action_type") == "series_cover_cache_created"
    ]
    assert len(cover_calls) == 1
    assert cover_calls[0].kwargs["payload"]["previous_cover_path"] == (
        "https://example.invalid/prior.jpg"
    )
    assert all(
        call.kwargs.get("action_type") != "series_created" for call in record_action.await_args_list
    )


@pytest.mark.asyncio
async def test_execute_existing_series_does_not_journal_preexisting_cover(
    db_session,
    tmp_path,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    existing = Series(
        title="Existing Cover",
        sort_title="existing cover",
        comicvine_id=166906,
        cover_path="/api/v1/series/999/cover",
    )
    db_session.add_all([job, existing])
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Existing Cover",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=166906,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Existing Cover 001.cbz")

    class SeriesServiceStub:
        async def add_from_import_review_targeted(self, _session, **_kwargs) -> Series:
            return existing

    record_action = AsyncMock()
    await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=AsyncMock(return_value=(1, 0)),
        record_action=record_action,
        log_event=AsyncMock(),
    )

    assert all(
        call.kwargs.get("action_type") != "series_cover_cache_created"
        for call in record_action.await_args_list
    )


@pytest.mark.asyncio
async def test_execute_existing_series_journals_import_created_series_folder(
    db_session,
    tmp_path,
    default_managed_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path / "imports"),
        source_type=ImportSourceType.FILESYSTEM,
        target_library_root_id=default_managed_root.id,
    )
    existing = Series(
        title="Pathless Existing",
        sort_title="pathless existing",
        comicvine_id=166907,
    )
    db_session.add_all([job, existing])
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Pathless Existing",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=166907,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Pathless Existing 001.cbz")
    library_root = Path(default_managed_root.path)
    series_folder = library_root / "Pathless Existing"
    series_folder.mkdir()

    class SeriesServiceStub:
        async def add_from_import_review_targeted(
            self,
            _session,
            *,
            import_series: ImportedSeries,
            **_kwargs,
        ) -> Series:
            existing.path = str(series_folder)
            existing.library_root_id = default_managed_root.id
            existing.preferred_library_root_id = default_managed_root.id
            import_series.diagnostics = {
                "series_folder_ownership": {
                    "schema_version": 1,
                    "folder_path": str(series_folder),
                    "ownership_boundary_path": str(library_root),
                    "created_directory_paths": [str(series_folder)],
                }
            }
            return existing

    record_action = AsyncMock()
    await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=AsyncMock(return_value=(1, 0)),
        record_action=record_action,
        log_event=AsyncMock(),
    )

    folder_calls = [
        call
        for call in record_action.await_args_list
        if call.kwargs.get("action_type") == "series_folder_created"
    ]
    assert len(folder_calls) == 1
    assert folder_calls[0].kwargs["payload"]["previous_series_path"] is None
    assert folder_calls[0].kwargs["payload"]["series_folder_ownership"][
        "created_directory_paths"
    ] == [str(series_folder)]


@pytest.mark.asyncio
async def test_execute_existing_series_orders_monitoring_folder_and_preexisting_cover_actions(
    db_session,
    tmp_path,
    default_managed_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        target_library_root_id=default_managed_root.id,
        search_on_add=True,
    )
    existing = Series(
        title="Existing Mutations",
        sort_title="existing mutations",
        comicvine_id=166908,
        monitored=False,
        cover_path="https://example.invalid/prior.jpg",
    )
    db_session.add_all([job, existing])
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Existing Mutations",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=166908,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Existing Mutations 001.cbz")
    library_root = Path(default_managed_root.path)
    series_folder = library_root / "Existing Mutations"
    series_folder.mkdir()

    class SeriesServiceStub:
        async def add_from_import_review_targeted(
            self,
            _session,
            *,
            import_series: ImportedSeries,
            **_kwargs,
        ) -> Series:
            existing.monitored = True
            existing.path = str(series_folder)
            existing.library_root_id = default_managed_root.id
            existing.preferred_library_root_id = default_managed_root.id
            existing.cover_path = f"/api/v1/series/{existing.id}/cover"
            import_series.diagnostics = {
                "series_folder_ownership": {
                    "schema_version": 1,
                    "folder_path": str(series_folder),
                    "ownership_boundary_path": str(library_root),
                    "created_directory_paths": [str(series_folder)],
                }
            }
            return existing

    record_action = AsyncMock()
    await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=AsyncMock(return_value=(1, 0)),
        record_action=record_action,
        log_event=AsyncMock(),
    )

    mutation_actions = [
        call.kwargs["action_type"]
        for call in record_action.await_args_list
        if call.kwargs.get("action_type")
        in {
            "series_monitoring_updated",
            "series_folder_created",
            "series_cover_path_updated",
            "series_cover_cache_created",
        }
    ]
    assert mutation_actions == [
        "series_monitoring_updated",
        "series_folder_created",
        "series_cover_path_updated",
    ]


def test_targeted_issue_summaries_preserve_review_issue_type() -> None:
    files = [
        ImportedFile(
            file_name="AL15 002.cbz",
            parsed_issue_number=2.0,
            matched_issue_cv_id=1149277,
            diagnostics={
                "target_issue_summary": {
                    "provider_id": "1149277",
                    "issue_number": 2.0,
                    "title": "Broken Dreams",
                    "release_date": None,
                    "cover_url": None,
                    "issue_type": IssueType.VOLUME.value,
                }
            },
        )
    ]

    summaries = _targeted_issue_summaries_for_import_files(files)

    assert len(summaries) == 1
    assert summaries[0].provider_id == "1149277"
    assert summaries[0].issue_number == 2.0
    assert summaries[0].title == "Broken Dreams"
    assert summaries[0].issue_type == IssueType.VOLUME.value


@pytest.mark.asyncio
async def test_execute_new_series_safety_block_keeps_series_imported_and_clears_error(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="The Joker Endgame Comic",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=84769,
        file_count=1,
        error_message="greenlet_spawn has not been called",
    )
    db_session.add(item)
    await db_session.flush()
    item_id = item.id
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path="/tmp/imports/The Joker Endgame.pdf",
            file_name="The Joker Endgame.pdf",
            file_size=1024,
            file_format="pdf",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.MATCHED,
        )
    )
    await db_session.flush()

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, *_args, **_kwargs) -> Series:
            series = Series(
                title="The Joker: Endgame",
                sort_title="joker endgame",
                year_start=2015,
                comicvine_id=84769,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(
        session,
        _job,
        import_item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        await session.rollback()
        import_item = await session.get(ImportedSeries, item_id)
        assert import_item is not None
        imp_file = (
            await session.execute(
                sa_select(ImportedFile).where(ImportedFile.import_series_id == import_item.id)
            )
        ).scalar_one()
        imp_file.status = ImportedFileStatus.SAFETY_BLOCKED
        imp_file.include_in_import = False
        imp_file.error_message = "File exceeded Pullbox's safe image processing limit."
        imp_file.diagnostics = {
            "safety_block": {
                "kind": "pillow_decompression_bomb",
                "reason": imp_file.error_message,
                "overrideable": True,
            }
        }
        import_item.diagnostics = {"safety_blocked_files": 1}
        await session.flush()
        await session.commit()
        return (0, 0)

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        record_action=AsyncMock(),
        log_event=AsyncMock(),
    )

    assert (files_ok, files_err, imported_count, failed_count, should_emit) == (0, 0, 1, 0, True)
    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.IMPORTED
    assert item.series_id is not None
    assert item.error_message is None


@pytest.mark.asyncio
async def test_execute_new_series_real_safety_block_handler_does_not_fail_series(
    db_session,
    tmp_path,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        search_on_add=True,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="The Joker Endgame Comic",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=84769,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    source = tmp_path / "The Joker Endgame.pdf"
    source.write_bytes(b"%PDF-1.7 placeholder")
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path=str(source),
            file_name=source.name,
            file_size=1024,
            file_format="pdf",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.MATCHED,
        )
    )
    await db_session.flush()

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, *_args, **_kwargs) -> Series:
            series = Series(
                title="The Joker: Endgame",
                sort_title="joker endgame",
                year_start=2015,
                comicvine_id=84769,
            )
            session.add(series)
            await session.flush()
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    comicvine_id=501005,
                    title="HC",
                    status=IssueStatus.WANTED,
                )
            )
            await session.flush()
            return series

    async def _prepare_file(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(
            "Archive worker failed during convert: DecompressionBombError: "
            "Image size exceeds limit of 178956970 pixels"
        )

    async def _process_series_files(
        session,
        process_job,
        import_item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        return await process_import_series_files(
            session,
            process_job,
            import_item,
            duplicate_mode=duplicate_mode,
            series_id_override=series_id_override,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=_prepare_file,
            build_comicinfo_payload=AsyncMock(return_value={}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=AsyncMock(),
            move_to_trash=AsyncMock(),
            report_file_progress=report_file_progress,
        )

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        record_action=AsyncMock(),
        log_event=AsyncMock(),
    )

    assert (files_ok, files_err, imported_count, failed_count, should_emit) == (0, 0, 1, 0, True)
    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.IMPORTED
    assert item.error_message is None


@pytest.mark.asyncio
async def test_catalog_hydration_scheduler_runs_background_hydration_one_at_a_time(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hydration.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        first_series = Series(
            title="First Hydration",
            sort_title="first hydration",
            year_start=2026,
            comicvine_id=1001,
            issue_catalog_state=IssueCatalogState.HYDRATING,
        )
        second_series = Series(
            title="Second Hydration",
            sort_title="second hydration",
            year_start=2026,
            comicvine_id=1002,
            issue_catalog_state=IssueCatalogState.HYDRATING,
        )
        session.add_all([first_series, second_series])
        await session.commit()

        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        active_hydrations = 0
        max_active_hydrations = 0
        prefetch_calls: list[int] = []

        class SeriesServiceStub:
            async def prefetch_comicvine_bundle(self, comicvine_id: int):
                nonlocal active_hydrations, max_active_hydrations
                active_hydrations += 1
                max_active_hydrations = max(max_active_hydrations, active_hydrations)
                prefetch_calls.append(comicvine_id)
                try:
                    if comicvine_id == 1001:
                        first_started.set()
                        await release_first.wait()
                    else:
                        second_started.set()
                        await release_second.wait()
                    return ({"comicvine_id": comicvine_id}, [])
                finally:
                    active_hydrations -= 1

            async def add_from_comicvine_prefetched(
                self,
                hydrate_session,
                *,
                comicvine_id: int,
                library_root_id: int | None,
                search_on_add: bool,
                series_meta,
                issue_summaries,
            ) -> Series:
                _ = library_root_id, search_on_add, series_meta, issue_summaries
                result = await hydrate_session.execute(
                    sa_select(Series).where(Series.comicvine_id == comicvine_id)
                )
                series = result.scalar_one()
                series.issue_catalog_state = IssueCatalogState.COMPLETE
                await hydrate_session.flush()
                return series

            async def hydrate_series_catalog(
                self,
                hydrate_session,
                series_id: int,
                *,
                search_on_add: bool,
            ) -> Series:
                _ = search_on_add
                series = await hydrate_session.get(Series, series_id)
                assert series is not None
                await self.prefetch_comicvine_bundle(int(series.comicvine_id or 0))
                series.issue_catalog_state = IssueCatalogState.COMPLETE
                await hydrate_session.flush()
                return series

        service = SeriesServiceStub()
        import_job_execution._schedule_catalog_hydration(
            session,
            series_service=service,
            series_id=first_series.id,
            search_on_add=False,
        )
        import_job_execution._schedule_catalog_hydration(
            session,
            series_service=service,
            series_id=second_series.id,
            search_on_add=False,
        )
        tasks = list(import_job_execution._catalog_hydration_tasks)
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert second_started.is_set() is False
            assert max_active_hydrations == 1

            release_first.set()
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert max_active_hydrations == 1

            release_second.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        finally:
            release_first.set()
            release_second.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            import_job_execution._catalog_hydration_tasks.difference_update(tasks)
            if hasattr(import_job_execution, "_catalog_hydration_semaphore"):
                import_job_execution._catalog_hydration_semaphore = None
            await engine.dispose()

        assert prefetch_calls == [1001, 1002]


@pytest.mark.asyncio
async def test_catalog_hydration_fetch_does_not_hold_database_connection(
    tmp_path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'hydration-pool.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        series = Series(
            title="Pool Friendly Hydration",
            sort_title="pool friendly hydration",
            year_start=2026,
            comicvine_id=2001,
            issue_catalog_state=IssueCatalogState.HYDRATING,
        )
        session.add(series)
        await session.commit()

        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        class SeriesServiceStub:
            async def prefetch_comicvine_bundle(self, comicvine_id: int):
                assert comicvine_id == 2001
                fetch_started.set()
                await release_fetch.wait()
                return ({"comicvine_id": comicvine_id}, [])

            async def add_from_comicvine_prefetched(
                self,
                hydrate_session,
                *,
                comicvine_id: int,
                library_root_id: int | None,
                search_on_add: bool,
                series_meta,
                issue_summaries,
            ) -> Series:
                _ = library_root_id, search_on_add, series_meta, issue_summaries
                refreshed = await hydrate_session.get(Series, series.id)
                assert refreshed is not None
                refreshed.issue_catalog_state = IssueCatalogState.COMPLETE
                await hydrate_session.flush()
                return refreshed

            async def hydrate_series_catalog(
                self,
                hydrate_session,
                series_id: int,
                *,
                search_on_add: bool,
            ) -> Series:
                _ = search_on_add
                refreshed = await hydrate_session.get(Series, series_id)
                assert refreshed is not None
                await self.prefetch_comicvine_bundle(int(refreshed.comicvine_id or 0))
                refreshed.issue_catalog_state = IssueCatalogState.COMPLETE
                await hydrate_session.flush()
                return refreshed

        import_job_execution._schedule_catalog_hydration(
            session,
            series_service=SeriesServiceStub(),
            series_id=series.id,
            search_on_add=False,
        )
        tasks = list(import_job_execution._catalog_hydration_tasks)
        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=1)
            async with session_factory() as reader:
                assert await reader.scalar(sa_select(Series.id).where(Series.id == series.id))

            release_fetch.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        finally:
            release_fetch.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            import_job_execution._catalog_hydration_tasks.difference_update(tasks)
            if hasattr(import_job_execution, "_catalog_hydration_semaphore"):
                import_job_execution._catalog_hydration_semaphore = None
            await engine.dispose()


@pytest.mark.asyncio
async def test_execute_new_series_commits_before_file_processing(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Fearscape",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=119851,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Fearscape 001.cbz")

    original_commit = db_session.commit
    commit_count = 0
    commit_seen_before_processing = False

    async def tracked_commit(*args, **kwargs):
        nonlocal commit_count, commit_seen_before_processing
        commit_count += 1
        commit_seen_before_processing = True
        return await original_commit(*args, **kwargs)

    async def _add_from_comicvine(_self, session, comicvine_id, *_args, **_kwargs):
        created = Series(
            title="Fearscape",
            sort_title="fearscape",
            comicvine_id=comicvine_id,
        )
        session.add(created)
        await session.flush()
        return created

    async def _process_series_files(*_args, **_kwargs):
        assert commit_seen_before_processing is True
        return (1, 0)

    with patch.object(db_session, "commit", side_effect=tracked_commit):
        files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
            db_session,
            job,
            item,
            imported_count=0,
            failed_count=0,
            series_service=type(
                "SeriesServiceStub",
                (),
                {"add_from_comicvine": _add_from_comicvine},
            )(),
            process_series_files=_process_series_files,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
        )

    assert files_ok == 1
    assert files_err == 0
    assert imported_count == 1
    assert failed_count == 0
    assert should_emit is True
    assert commit_count >= 1


@pytest.mark.asyncio
async def test_execute_new_series_persists_ownership_link_before_file_cancellation(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Cancelled Series",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=326,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    item_id = item.id
    await _add_matched_file(db_session, job, item, file_name="Cancelled Series 001.cbz")

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Cancelled Series",
                sort_title="cancelled series",
                comicvine_id=326,
            )
            session.add(series)
            await session.flush()
            return series

    async def cancel_during_files(*_args, **_kwargs) -> tuple[int, int]:
        raise JobCancelledError("cancelled")

    with pytest.raises(JobCancelledError):
        await _execute_new_series(
            db_session,
            job,
            item,
            imported_count=0,
            failed_count=0,
            series_service=SeriesServiceStub(),
            process_series_files=cancel_during_files,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
        )

    await db_session.rollback()
    persisted_item = await db_session.get(ImportedSeries, item_id)
    assert persisted_item is not None
    assert persisted_item.series_id is not None


@pytest.mark.asyncio
async def test_execute_new_series_fails_when_no_files_are_eligible(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="The Joker - Endgame",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=84769,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()

    series_service = AsyncMock()
    process_series_files = AsyncMock(return_value=(0, 0))

    log_event = AsyncMock()

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=series_service,
        process_series_files=process_series_files,
        record_action=AsyncMock(),
        log_event=log_event,
    )

    assert (files_ok, files_err, imported_count, failed_count, should_emit) == (0, 0, 0, 1, True)
    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.FAILED
    assert item.error_message == "No eligible files available for import"
    series_service.add_from_comicvine.assert_not_awaited()
    process_series_files.assert_not_awaited()
    log_event.assert_any_await(
        db_session,
        job.id,
        "ERROR",
        "import_series_no_eligible_files",
        message="No eligible files available to import for The Joker - Endgame",
        raw_series_name="The Joker - Endgame",
        cv_id=84769,
        series_id=None,
    )


@pytest.mark.asyncio
async def test_execute_import_job_prefetches_upcoming_new_series(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    items: list[ImportedSeries] = []
    for idx, cv_id in enumerate((110930, 110931, 110932), start=1):
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Series {idx}",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=cv_id,
            file_count=1,
        )
        db_session.add(item)
        items.append(item)
    await db_session.flush()
    for item in items:
        await _add_matched_file(db_session, job, item, file_name=f"{item.raw_series_name} 001.cbz")

    prefetch_started: list[int] = []
    add_calls: list[int] = []
    observations: dict[str, list[int]] = {}

    class SeriesServiceStub:
        async def prefetch_comicvine_bundle(self, comicvine_id: int):
            prefetch_started.append(comicvine_id)
            await asyncio.sleep(0)
            return ({"comicvine_id": comicvine_id}, [{"issue": comicvine_id}])

        async def add_from_comicvine_prefetched(
            self,
            session,
            *,
            comicvine_id: int,
            library_root_id: int | None,
            search_on_add: bool,
            series_meta,
            issue_summaries,
        ) -> Series:
            _ = library_root_id, search_on_add
            if not add_calls:
                observations["prefetched_before_first_add"] = list(prefetch_started)
            add_calls.append(comicvine_id)
            series = Series(
                title=f"Series {comicvine_id}",
                sort_title=f"series {comicvine_id}",
                year_start=2020,
                comicvine_id=comicvine_id,
            )
            session.add(series)
            await session.flush()
            assert series_meta["comicvine_id"] == comicvine_id
            assert issue_summaries == [{"issue": comicvine_id}]
            return series

        async def add_from_comicvine(self, *_args, **_kwargs) -> Series:
            raise AssertionError("prefetched import path should be used when available")

    async def _process_series_files(*_args, **_kwargs) -> tuple[int, int]:
        return (0, 0)

    async def _emit_progress(*_args, **_kwargs) -> None:
        return None

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=_emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=None,
    )

    assert add_calls == [110930, 110931, 110932]
    assert observations["prefetched_before_first_add"][:2] == [110930, 110931]


@pytest.mark.asyncio
async def test_execute_import_job_defers_catalog_hydration_until_after_file_groups(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        search_on_add=True,
    )
    db_session.add(job)
    await db_session.flush()

    items: list[ImportedSeries] = []
    for raw_name, cv_id in (("First Series", 221001), ("Second Series", 221002)):
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=raw_name,
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=cv_id,
            cv_title=raw_name,
            cv_year=2026,
            file_count=1,
        )
        db_session.add(item)
        items.append(item)
    await db_session.flush()
    for item in items:
        await _add_matched_file(db_session, job, item, file_name=f"{item.raw_series_name} 001.cbz")

    scheduled_hydrations: list[tuple[int, bool]] = []
    processed_groups: list[str] = []
    hydration_counts_at_file_start: list[int] = []
    created_series_ids: list[int] = []
    activate_policy = AsyncMock(return_value=None)

    def fake_schedule_catalog_hydration(
        _session,
        *,
        series_service,
        series_id: int,
        search_on_add: bool,
    ) -> None:
        _ = series_service
        scheduled_hydrations.append((series_id, search_on_add))

    class SeriesServiceStub:
        async def add_from_import_review_targeted(
            self,
            session,
            *,
            import_series: ImportedSeries,
            library_root_id: int | None,
            search_on_add: bool,
            issue_summaries: list[object],
        ) -> Series:
            _ = library_root_id, search_on_add, issue_summaries
            series = Series(
                title=import_series.cv_title or import_series.raw_series_name,
                sort_title=(import_series.cv_title or import_series.raw_series_name).lower(),
                year_start=import_series.cv_year,
                comicvine_id=import_series.cv_id,
                issue_catalog_state=IssueCatalogState.HYDRATING,
            )
            session.add(series)
            await session.flush()
            created_series_ids.append(series.id)
            return series

    async def _process_series_files(
        _session,
        _job,
        item: ImportedSeries,
        **_kwargs,
    ) -> tuple[int, int]:
        processed_groups.append(item.raw_series_name)
        hydration_counts_at_file_start.append(len(scheduled_hydrations))
        return (1, 0)

    monkeypatch.setattr(
        import_job_execution,
        "_schedule_catalog_hydration",
        fake_schedule_catalog_hydration,
    )
    monkeypatch.setattr(
        import_job_execution,
        "activate_future_root_policy",
        activate_policy,
    )

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=None,
    )

    assert processed_groups == ["First Series", "Second Series"]
    assert hydration_counts_at_file_start == [0, 0]
    assert scheduled_hydrations == [(series_id, True) for series_id in created_series_ids]
    assert [
        call.kwargs["successful_registration_count"] for call in activate_policy.await_args_list
    ] == [1, 2]


@pytest.mark.asyncio
async def test_execute_import_job_emits_metadata_heartbeat_during_slow_prefetch(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(import_job_execution, "_METADATA_PROGRESS_HEARTBEAT_SECONDS", 0.01)
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    db_session.add(
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name="2000AD",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=19752,
            file_count=1,
        )
    )
    await db_session.flush()

    release_prefetch = asyncio.Event()
    progress_events = []
    durable_snapshots: list[dict[str, object]] = []

    class SeriesServiceStub:
        async def prefetch_comicvine_bundle(self, comicvine_id: int):
            assert comicvine_id == 19752
            await release_prefetch.wait()
            return ({"comicvine_id": comicvine_id}, [{"issue": comicvine_id}])

        async def add_from_comicvine_prefetched(
            self,
            session,
            *,
            comicvine_id: int,
            library_root_id: int | None,
            search_on_add: bool,
            series_meta,
            issue_summaries,
        ) -> Series:
            _ = library_root_id, search_on_add
            series = Series(
                title="2000 AD",
                sort_title="2000 ad",
                year_start=1977,
                comicvine_id=comicvine_id,
            )
            session.add(series)
            await session.flush()
            assert series_meta["comicvine_id"] == comicvine_id
            assert issue_summaries == [{"issue": comicvine_id}]
            return series

        async def add_from_comicvine(self, *_args, **_kwargs) -> Series:
            raise AssertionError("prefetched import path should be used when available")

    async def _process_series_files(*_args, **_kwargs) -> tuple[int, int]:
        return (0, 0)

    async def _capture(event):
        captured = event.model_copy(deep=True)
        progress_events.append(captured)
        if captured.message.startswith("Still fetching ComicVine metadata"):
            await db_session.refresh(job)
            durable_snapshots.append(dict(job.progress_snapshot or {}))
            release_prefetch.set()

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    metadata_messages = [
        event.message
        for event in progress_events
        if event.current_series_name == "2000AD" and event.current_file_name is None
    ]
    metadata_events = [
        event
        for event in progress_events
        if event.current_series_name == "2000AD" and event.current_file_name is None
    ]
    assert any(
        message.startswith("Fetching ComicVine metadata for 2000AD")
        for message in metadata_messages
    )
    assert any(
        message.startswith("Still fetching ComicVine metadata for 2000AD")
        for message in metadata_messages
    )
    assert any(message == "Preparing series records for 2000AD..." for message in metadata_messages)
    metadata_revisions = [
        event.progress_revision for event in progress_events if event.message in metadata_messages
    ]
    assert metadata_revisions == sorted(metadata_revisions)
    assert len(set(metadata_revisions)) == len(metadata_revisions)
    assert durable_snapshots
    assert durable_snapshots[-1]["message"].startswith("Fetching ComicVine metadata")
    assert durable_snapshots[-1]["current_series_name"] == "2000AD"
    assert any(
        event.ephemeral_progress
        and event.current_item_stage == "metadata_fetch_wait"
        and event.message.startswith("Still fetching ComicVine metadata")
        for event in metadata_events
    )
    assert {
        (event.current_item_kind, event.current_item_stage, event.current_item_stage_label)
        for event in metadata_events
    } >= {
        ("series", "metadata_fetch", "Fetching ComicVine metadata"),
        ("series", "metadata_fetch_wait", "Fetching ComicVine metadata"),
        ("series", "series_records", "Preparing series records"),
    }
    assert all(event.current_item_progress_pct is not None for event in metadata_events)


@pytest.mark.asyncio
async def test_execute_import_job_emits_metadata_progress_without_prefetch_support(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Persephone",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=110930,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Persephone.2022.pdf")

    progress_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            assert any(
                event.message.startswith("Fetching ComicVine metadata for Persephone")
                for event in progress_events
            )
            series = Series(
                title="Persephone",
                sort_title="persephone",
                year_start=2022,
                comicvine_id=110930,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(*_args, **_kwargs) -> tuple[int, int]:
        return (0, 0)

    async def _capture(event):
        progress_events.append(event.model_copy(deep=True))

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    assert any(
        event.current_series_name == "Persephone"
        and event.message.startswith("Fetching ComicVine metadata for Persephone")
        for event in progress_events
    )


@pytest.mark.asyncio
async def test_execute_import_job_emits_active_file_progress(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Persephone",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=110930,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Persephone.2022.pdf")

    progress_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Persephone",
                sort_title="persephone",
                year_start=2022,
                comicvine_id=110930,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(
        _session,
        _job,
        _item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        assert duplicate_mode is False
        assert series_id_override is not None
        assert report_file_progress is not None
        imp_file = ImportedFile(
            id=401,
            import_job_id=job.id,
            import_series_id=_item.id,
            file_path="/tmp/Persephone.2022.pdf",
            file_name="Persephone.2022.pdf",
            file_size=10,
            file_format="pdf",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=2,
            stage="rendering",
            current=5,
            total=10,
            unit="pages",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=2,
            stage="transferring",
            current=50,
            total=100,
            unit="bytes",
        )
        return (2, 0)

    async def _emit_progress(_session, _job, event, progress_callback):
        if progress_callback is not None:
            await progress_callback(event)

    async def _capture(event):
        progress_events.append(event.model_copy(deep=True))

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=_emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    active_file_events = [
        event
        for event in progress_events
        if getattr(event, "current_file_name", None) == "Persephone.2022.pdf"
    ]
    assert active_file_events
    assert any(event.current_file_stage == "rendering" for event in active_file_events)
    assert any(event.current_file_stage == "transferring" for event in active_file_events)
    assert any(0 < event.progress < 100 for event in active_file_events)


@pytest.mark.asyncio
async def test_execute_import_job_persists_only_stage_boundary_file_progress(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Wasted Space",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=165113,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(
        db_session,
        job,
        item,
        file_name="Wasted.Space.The.Cosmic.Collection.2023.pdf",
    )

    persisted_events = []
    streamed_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Wasted Space",
                sort_title="wasted space",
                year_start=2023,
                comicvine_id=165113,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(
        _session,
        _job,
        _item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        assert duplicate_mode is False
        assert series_id_override is not None
        assert report_file_progress is not None
        imp_file = ImportedFile(
            id=403,
            import_job_id=job.id,
            import_series_id=_item.id,
            file_path="/tmp/Wasted.Space.The.Cosmic.Collection.2023.pdf",
            file_name="Wasted.Space.The.Cosmic.Collection.2023.pdf",
            file_size=10,
            file_format="pdf",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="rendering",
            current=1,
            total=10,
            unit="pages",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="rendering",
            current=5,
            total=10,
            unit="pages",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="rendering",
            current=10,
            total=10,
            unit="pages",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="transferring",
            current=50,
            total=100,
            unit="bytes",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="rewriting",
            current=50,
            total=100,
            unit="entries",
        )
        return (1, 0)

    async def _emit_progress(_session, _job, event, progress_callback):
        persisted_events.append(event.model_copy(deep=True))
        if progress_callback is not None:
            await progress_callback(event)

    async def _capture(event):
        streamed_events.append(event.model_copy(deep=True))

    async def _emit_active_file_progress_stub(*_args, **kwargs):
        await _emit_progress(
            None,
            None,
            kwargs["event"],
            kwargs["progress_callback"],
        )

    with patch(
        "pullbox.services.import_job_execution._emit_active_file_progress",
        side_effect=_emit_active_file_progress_stub,
    ):
        await execute_import_job(
            db_session,
            job.id,
            series_service=SeriesServiceStub(),
            process_series_files=_process_series_files,
            raise_if_cancelled=AsyncMock(),
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            emit_progress=_emit_progress,
            estimate_remaining_seconds=lambda *_args, **_kwargs: None,
            maybe_slow_item_delay=AsyncMock(),
            progress_callback=_capture,
        )

    rendering_persisted = [
        event.current_file_progress_current
        for event in persisted_events
        if getattr(event, "current_file_stage", None) == "rendering"
    ]
    rendering_streamed = [
        event.current_file_progress_current
        for event in streamed_events
        if getattr(event, "current_file_stage", None) == "rendering"
    ]

    assert rendering_streamed == [1, 5, 10]
    assert rendering_persisted == [1, 10]
    assert [
        event.current_file_stage
        for event in streamed_events
        if getattr(event, "current_file_stage", None) in {"transferring", "rewriting"}
    ] == ["transferring", "rewriting"]
    assert not [
        event
        for event in persisted_events
        if getattr(event, "current_file_stage", None) in {"transferring", "rewriting"}
    ]


@pytest.mark.asyncio
async def test_active_file_progress_lock_falls_back_to_live_event(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()

    event = ImportProgressEvent(
        job_id=job.id,
        status=ImportJobStatus.IMPORTING,
        mode="import",
        phase="importing",
        progress=44,
        message="Processing King Dracula 04 (of 04) (2026).cbr",
        current_file_name="King Dracula 04 (of 04) (2026).cbr",
        current_file_stage="preparing",
        current_file_progress_current=0,
        current_file_progress_total=1,
        current_file_progress_pct=0,
        current_file_progress_unit="steps",
    )
    streamed_events: list[ImportProgressEvent] = []

    async def _capture(progress_event: ImportProgressEvent) -> None:
        streamed_events.append(progress_event.model_copy(deep=True))

    async def _raise_locked(*_args, **_kwargs) -> None:
        raise OperationalError(
            "UPDATE import_jobs",
            {},
            Exception("database is locked"),
        )

    with patch("pullbox.services.import_workflow_state.emit_progress", side_effect=_raise_locked):
        await import_job_execution._emit_active_file_progress(
            db_session,
            None,
            job_id=job.id,
            event=event,
            progress_callback=_capture,
        )

    assert [progress_event.current_file_stage for progress_event in streamed_events] == [
        "preparing"
    ]
    assert streamed_events[0].current_file_name == "King Dracula 04 (of 04) (2026).cbr"


@pytest.mark.asyncio
async def test_execute_import_job_keeps_live_only_finalizing_progress_monotonic(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Dead Space Salvage",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=38683,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Dead.Space.Salvage.2010.pdf")

    progress_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Dead Space Salvage",
                sort_title="dead space salvage",
                year_start=2010,
                comicvine_id=38683,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(
        _session,
        _job,
        _item,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        assert duplicate_mode is False
        assert series_id_override is not None
        assert report_file_progress is not None
        imp_file = ImportedFile(
            id=402,
            import_job_id=job.id,
            import_series_id=_item.id,
            file_path="/tmp/Dead.Space.Salvage.2010.pdf",
            file_name="Dead.Space.Salvage.2010.pdf",
            file_size=10,
            file_format="pdf",
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="finalizing",
            current=0,
            total=4,
            unit="steps",
            live_only=True,
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="finalizing",
            current=3,
            total=4,
            unit="steps",
            live_only=True,
        )
        await report_file_progress(
            imp_file=imp_file,
            file_index=1,
            total_files=1,
            stage="finalizing",
            current=4,
            total=4,
            unit="steps",
        )
        return (1, 0)

    async def _emit_progress(_session, _job, event, progress_callback):
        if progress_callback is not None:
            await progress_callback(event)

    async def _capture(event):
        progress_events.append(event.model_copy(deep=True))

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=_emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    finalizing_events = [
        event
        for event in progress_events
        if getattr(event, "current_file_stage", None) == "finalizing"
    ]
    assert [event.current_file_progress_current for event in finalizing_events] == [0, 3, 4]
    assert [event.progress_revision for event in finalizing_events] == sorted(
        event.progress_revision for event in finalizing_events
    )
    assert finalizing_events[-1].progress_revision > finalizing_events[0].progress_revision
    processed_group_events = [
        event for event in progress_events if event.message == "Processed 1/1 review groups"
    ]
    assert processed_group_events
    assert processed_group_events[-1].current_file_stage is None
    assert processed_group_events[-1].progress_revision > finalizing_events[-1].progress_revision


@pytest.mark.asyncio
async def test_execute_import_job_survives_series_failure_after_rollback(db_session) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    db_session.add(
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Broken Series",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=999001,
            file_count=1,
        )
    )
    await db_session.flush()

    progress_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Broken Series",
                sort_title="broken series",
                year_start=2024,
                comicvine_id=999001,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(*_args, **_kwargs) -> tuple[int, int]:
        raise RuntimeError("boom")

    async def _emit_progress(_session, _job, event, progress_callback):
        if progress_callback is not None:
            await progress_callback(event)

    async def _capture(event):
        progress_events.append(event.model_copy(deep=True))

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=_emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    await db_session.refresh(job)
    assert job.status == ImportJobStatus.COMPLETED
    assert job.series_failed == 1
    assert progress_events


@pytest.mark.asyncio
async def test_execute_new_series_survives_file_loop_rollback_without_orm_reloads(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Wasted Space",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=165113,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Wasted.Space.001.cbz")

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Wasted Space",
                sort_title="wasted space",
                year_start=2023,
                comicvine_id=165113,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(session, *_args, **_kwargs) -> tuple[int, int]:
        item_id = item.id
        await session.rollback()
        reloaded_item = await session.get(ImportedSeries, item_id)
        assert reloaded_item is not None
        reloaded_item.files_failed = 1
        await session.commit()
        return (0, 1)

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        record_action=AsyncMock(),
        log_event=AsyncMock(),
    )

    assert (files_ok, files_err, imported_count, failed_count, should_emit) == (0, 1, 1, 0, True)
    await db_session.commit()
    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.IMPORTED
    assert item.series_id is not None


@pytest.mark.asyncio
async def test_execute_new_series_uses_scalar_series_id_after_file_failure_rollback(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    db_session.add(
        Series(
            id=321,
            comicvine_id=321000,
            title="Necronomicon",
            sort_title="necronomicon",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
        )
    )
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Necronomicon",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=22863,
        file_count=4,
    )
    db_session.add(item)
    await db_session.flush()
    item_id = item.id
    await _add_matched_file(db_session, job, item, file_name="Necronomicon 004.cbz")

    class SeriesStub:
        def __init__(self) -> None:
            self.locked = False
            self.id_accesses = 0

        @property
        def id(self) -> int:
            if self.locked:
                raise AssertionError("Series ORM state was accessed after file rollback")
            self.id_accesses += 1
            return 321

    series_stub = SeriesStub()

    class SeriesServiceStub:
        async def add_from_comicvine(self, *_args, **_kwargs) -> SeriesStub:
            return series_stub

    async def _process_series_files(
        session,
        *_args,
        series_id_override: int | None = None,
        **_kwargs,
    ) -> tuple[int, int]:
        assert series_id_override == 321
        # A per-file failure rolls back the session, which expires ORM instances.
        # The import summary must not touch the original series object afterward.
        series_stub.locked = True
        await session.rollback()
        reloaded_item = await session.get(ImportedSeries, item_id)
        assert reloaded_item is not None
        reloaded_item.files_imported = 3
        reloaded_item.files_failed = 1
        await session.commit()
        return (3, 1)

    files_ok, files_err, imported_count, failed_count, should_emit = await _execute_new_series(
        db_session,
        job,
        item,
        imported_count=0,
        failed_count=0,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        record_action=AsyncMock(),
        log_event=AsyncMock(),
    )

    assert (files_ok, files_err, imported_count, failed_count, should_emit) == (3, 1, 1, 0, True)
    assert series_stub.id_accesses == 1
    await db_session.commit()
    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.IMPORTED
    assert item.series_id == 321


@pytest.mark.asyncio
async def test_execute_import_job_progress_uses_reloaded_series_after_file_loop_rollback(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Wasted Space",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=165113,
        file_count=1,
    )
    db_session.add(item)
    await db_session.flush()
    await _add_matched_file(db_session, job, item, file_name="Wasted.Space.001.cbz")

    progress_events = []

    class SeriesServiceStub:
        async def add_from_comicvine(self, session, **_kwargs) -> Series:
            series = Series(
                title="Wasted Space",
                sort_title="wasted space",
                year_start=2023,
                comicvine_id=165113,
            )
            session.add(series)
            await session.flush()
            return series

    async def _process_series_files(session, _job, item, **_kwargs) -> tuple[int, int]:
        item_id = item.id
        await session.rollback()
        reloaded_item = await session.get(ImportedSeries, item_id)
        assert reloaded_item is not None
        reloaded_item.files_failed = 1
        await session.commit()
        return (0, 1)

    async def _emit_progress(_session, _job, event, progress_callback):
        if progress_callback is not None:
            await progress_callback(event)

    async def _capture(event):
        progress_events.append(event.model_copy(deep=True))

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=_emit_progress,
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=_capture,
    )

    processed_group_events = [
        event for event in progress_events if event.message == "Processed 1/1 review groups"
    ]
    assert processed_group_events
    assert processed_group_events[-1].current_series == "Wasted Space"
    assert processed_group_events[-1].current_series_status == ImportSeriesStatus.IMPORTED
    await db_session.refresh(job)
    assert job.status == ImportJobStatus.COMPLETED
    assert job.series_imported == 1


@pytest.mark.asyncio
async def test_execute_import_job_continues_prefetch_window_after_series_failure(
    db_session,
) -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()

    broken_item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Broken Series",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=999101,
        file_count=1,
    )
    recovered_item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Recovered Series",
        status=ImportSeriesStatus.CONFIRMED,
        cv_id=999102,
        file_count=1,
    )
    db_session.add_all([broken_item, recovered_item])
    await db_session.flush()
    await _add_matched_file(db_session, job, broken_item, file_name="Broken Series 001.cbz")
    await _add_matched_file(db_session, job, recovered_item, file_name="Recovered Series 001.cbz")

    add_calls: list[int] = []

    class SeriesServiceStub:
        async def prefetch_comicvine_bundle(self, comicvine_id: int):
            return ({"comicvine_id": comicvine_id}, [{"issue": comicvine_id}])

        async def add_from_comicvine_prefetched(
            self,
            session,
            *,
            comicvine_id: int,
            library_root_id: int | None,
            search_on_add: bool,
            series_meta,
            issue_summaries,
        ) -> Series:
            _ = library_root_id, search_on_add, series_meta, issue_summaries
            add_calls.append(comicvine_id)
            series = Series(
                title=f"Series {comicvine_id}",
                sort_title=f"series {comicvine_id}",
                year_start=2024,
                comicvine_id=comicvine_id,
            )
            session.add(series)
            await session.flush()
            return series

        async def add_from_comicvine(self, *_args, **_kwargs) -> Series:
            raise AssertionError("prefetched path should be used")

    async def _process_series_files(_session, _job, item, **_kwargs) -> tuple[int, int]:
        if item.raw_series_name == "Broken Series":
            raise RuntimeError("boom")
        return (1, 0)

    await execute_import_job(
        db_session,
        job.id,
        series_service=SeriesServiceStub(),
        process_series_files=_process_series_files,
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args, **_kwargs: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=AsyncMock(),
    )

    await db_session.refresh(job)
    assert add_calls == [999101, 999102]
    assert job.status == ImportJobStatus.COMPLETED
    assert job.series_imported == 1
    assert job.series_failed == 1


def test_active_file_progress_keeps_rewriting_below_100_until_finalizing() -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    job.move_to_library = True
    job.update_embedded_comicinfo_from_match = True
    imp_file = ImportedFile(
        file_path="/tmp/Persephone.2022.Hybrid.Comic.eBook-BitBook.pdf",
        file_name="Persephone.2022.Hybrid.Comic.eBook-BitBook.pdf",
        file_size=10,
        file_format="pdf",
    )

    settings = _ActiveFileProgressSettings(
        move_to_library=bool(job.move_to_library),
        convert_to_preferred_format=bool(job.convert_to_preferred_format),
        update_embedded_comicinfo_from_match=bool(job.update_embedded_comicinfo_from_match),
    )
    rewriting_pct = _active_file_progress_pct(settings, imp_file, "rewriting", 1, 1)
    finalizing_pct = _active_file_progress_pct(settings, imp_file, "finalizing", 1, 1)

    assert rewriting_pct < 100
    assert finalizing_pct == 100


def test_active_file_progress_keeps_chunked_pdf_conversion_monotonic() -> None:
    job = ImportJob(
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    job.move_to_library = True
    job.convert_to_preferred_format = True
    job.update_embedded_comicinfo_from_match = True
    imp_file = ImportedFile(
        file_path="/tmp/Wasted.Space.The.Cosmic.Collection.2023.pdf",
        file_name="Wasted.Space.The.Cosmic.Collection.2023.pdf",
        file_size=10,
        file_format="pdf",
    )

    settings = _ActiveFileProgressSettings(
        move_to_library=bool(job.move_to_library),
        convert_to_preferred_format=bool(job.convert_to_preferred_format),
        update_embedded_comicinfo_from_match=bool(job.update_embedded_comicinfo_from_match),
    )

    progress_points = [
        _active_file_progress_pct(settings, imp_file, "rendering", 1, 675),
        _active_file_progress_pct(settings, imp_file, "encoding", 1, 675),
        _active_file_progress_pct(settings, imp_file, "rendering", 2, 675),
        _active_file_progress_pct(settings, imp_file, "encoding", 2, 675),
        _active_file_progress_pct(settings, imp_file, "rendering", 100, 675),
        _active_file_progress_pct(settings, imp_file, "encoding", 100, 675),
    ]

    assert progress_points == sorted(progress_points)
    assert progress_points[1] >= progress_points[0]
    assert progress_points[2] >= progress_points[1]
    assert progress_points[-1] > progress_points[0]
