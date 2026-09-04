"""Tests for Step 4 import execution progress helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event

from pullbox.core.exceptions import JobPausedError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_active_file_progress import ActiveFileProgressSettings
from pullbox.services.import_job_execution_progress import (
    await_prefetch_with_metadata_progress,
    build_import_group_progress_plans,
    build_report_file_progress_callback,
    build_series_metadata_progress_emitter,
)
from pullbox.services.import_job_execution_types import ExecutionItemPlan
from pullbox.services.import_progress_runtime import (
    ImportGroupProgressPlan,
    ImportProgressFileProfile,
    ImportProgressSettings,
    import_group_progress_plan,
)


def _job() -> ImportJob:
    return ImportJob(
        id=42,
        source_path="/tmp/imports",
        status=ImportJobStatus.IMPORTING,
    )


def _file() -> ImportedFile:
    return ImportedFile(
        id=99,
        import_job_id=42,
        import_series_id=7,
        file_path="/imports/Alpha 001.cbz",
        file_name="Alpha 001.cbz",
        file_size=1024,
    )


def _callback_kwargs(**overrides: Any) -> dict[str, Any]:
    settings = ImportProgressSettings(
        move_to_library=True,
        convert_to_preferred_format=True,
        update_embedded_comicinfo_from_match=True,
    )
    plan = import_group_progress_plan(
        settings,
        [
            ImportProgressFileProfile(
                file_id=99,
                file_path="/imports/Alpha 001.cbz",
                file_size=1024,
            )
        ],
    )
    kwargs: dict[str, Any] = {
        "session": object(),
        "job_id": 42,
        "job": _job(),
        "job_started_at": datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
        "work_started_at": datetime.now(UTC) - timedelta(seconds=20),
        "progress_callback": object(),
        "progress_session_factory": None,
        "group_progress_plans": {7: plan},
        "shared_progress_settings": settings,
        "group_progress_weights": [plan.total_weight],
        "group_index": 0,
        "total_groups": 1,
        "series_id": 7,
        "series_name": "Alpha",
        "series_found": 3,
        "stats": {
            "series_imported": 1,
            "series_failed": 2,
            "total_files_imported": 4,
            "total_files_failed": 5,
        },
        "progress_state": {
            "file_id": None,
            "stage": None,
            "pct": None,
            "emitted_at": 0.0,
        },
        "revision_state": {"value": 11},
        "active_file_progress_settings": ActiveFileProgressSettings(
            move_to_library=True,
            convert_to_preferred_format=True,
            update_embedded_comicinfo_from_match=True,
        ),
        "monotonic_time": lambda: 10.0,
        "active_file_progress_emit_interval_seconds": 0.2,
        "live_only_active_file_stages": frozenset({"transferring", "rewriting"}),
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("large_group", [False, True])
@pytest.mark.parametrize("live_only", [False, True])
async def test_file_eta_is_available_before_display_reaches_one_percent(
    large_group: bool, live_only: bool
) -> None:
    events = []

    async def capture_active(*_args, **kwargs):
        events.append(kwargs["event"])

    async def capture_live(_job, event, **_kwargs):
        events.append(event)

    kwargs = _callback_kwargs(
        work_started_at=datetime.now(UTC) - timedelta(seconds=20),
        emit_active_file_progress=capture_active,
        emit_live_progress=capture_live,
    )
    if large_group:
        plan = ImportGroupProgressPlan(2.0, tuple((99 + idx, 3.5) for idx in range(2000)))
        kwargs["group_progress_plans"] = {7: plan}
        kwargs["group_progress_weights"] = [plan.total_weight]
    else:
        kwargs["group_progress_weights"] *= 50_000
        kwargs["total_groups"] = 50_000
    callback = build_report_file_progress_callback(**kwargs)
    await callback(
        imp_file=_file(),
        file_index=1,
        total_files=2000 if large_group else 1,
        stage="finalizing",
        current=1,
        total=1,
        unit="file",
        live_only=live_only,
    )

    assert events[-1].progress == 0
    assert events[-1].estimated_seconds_remaining is not None
    assert events[-1].estimated_seconds_remaining > 0


@pytest.mark.parametrize("live_only", [False, True])
async def test_metadata_eta_is_available_before_display_reaches_one_percent(
    live_only: bool,
) -> None:
    events = []
    kwargs = _callback_kwargs()

    async def capture(_session, _job, event, _callback):
        events.append(event)

    async def capture_live(_job, event, **_kwargs):
        events.append(event)

    emitter = build_series_metadata_progress_emitter(
        session=kwargs["session"],
        job_id=42,
        job=kwargs["job"],
        job_started_at=datetime.now(UTC) - timedelta(seconds=20),
        work_started_at=datetime.now(UTC) - timedelta(seconds=20),
        progress_callback=kwargs["progress_callback"],
        emit_progress=capture,
        emit_live_progress=capture_live,
        group_progress_plans=kwargs["group_progress_plans"],
        shared_progress_settings=kwargs["shared_progress_settings"],
        group_progress_weights=kwargs["group_progress_weights"] * 50_000,
        stats=lambda: kwargs["stats"],
        series_found=50_000,
        revision_state=kwargs["revision_state"],
    )
    await emitter(
        group_index=1,
        total_groups=50_000,
        series_id=7,
        series_name="Alpha",
        message="Preparing series records",
        current_item_stage="series_records",
        current_item_progress_pct=10,
        live_only=live_only,
    )

    assert events[-1].progress == 0
    assert events[-1].estimated_seconds_remaining is not None
    assert events[-1].estimated_seconds_remaining > 0


@pytest.mark.asyncio
async def test_build_import_group_progress_plans_batches_large_selections(
    async_engine,
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()

    series_items = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Scale Series {index:04d}",
            status=ImportSeriesStatus.CONFIRMED,
        )
        for index in range(600)
    ]
    db_session.add_all(series_items)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path=f"/imports/{item.raw_series_name}.cbz",
                file_name=f"{item.raw_series_name}.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
            )
            for item in series_items
        ]
    )
    await db_session.flush()
    execution_items = [
        ExecutionItemPlan(
            mode="new",
            item_id=item.id,
            raw_series_name=item.raw_series_name,
            cv_id=None,
            existing_series_id=None,
        )
        for item in series_items
    ]

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

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
    try:
        plans = await build_import_group_progress_plans(
            db_session,
            execution_items,
            ImportProgressSettings(
                move_to_library=True,
                convert_to_preferred_format=False,
                update_embedded_comicinfo_from_match=False,
            ),
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

    assert len(plans) == 600
    assert all(len(plan.file_weights) == 1 for plan in plans.values())
    assert len(selects) <= 2, f"progress preparation issued {len(selects)} SELECTs"


@pytest.mark.asyncio
async def test_build_import_group_progress_plans_preserves_file_selection_rules(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    new_item = ImportedSeries(
        import_job=job,
        raw_series_name="New Series",
        status=ImportSeriesStatus.CONFIRMED,
    )
    duplicate_item = ImportedSeries(
        import_job=job,
        raw_series_name="Existing Series",
        status=ImportSeriesStatus.DUPLICATE,
    )
    db_session.add_all([job, new_item, duplicate_item])
    await db_session.flush()

    new_match = ImportedFile(
        import_job_id=job.id,
        import_series_id=new_item.id,
        file_path="/imports/New 001.cbz",
        file_name="New 001.cbz",
        file_size=101,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
    )
    new_preferred_conflict = ImportedFile(
        import_job_id=job.id,
        import_series_id=new_item.id,
        file_path="/imports/New 002 preferred.cbz",
        file_name="New 002 preferred.cbz",
        file_size=102,
        file_format="cbz",
        status=ImportedFileStatus.CONFLICT,
        is_preferred=True,
    )
    duplicate_selected = ImportedFile(
        import_job_id=job.id,
        import_series_id=duplicate_item.id,
        file_path="/imports/Existing 001.cbz",
        file_name="Existing 001.cbz",
        file_size=201,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
    )
    duplicate_unselected = ImportedFile(
        import_job_id=job.id,
        import_series_id=duplicate_item.id,
        file_path="/imports/Existing 002.cbz",
        file_name="Existing 002.cbz",
        file_size=202,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=False,
    )
    db_session.add_all(
        [
            new_match,
            new_preferred_conflict,
            duplicate_selected,
            duplicate_unselected,
        ]
    )
    await db_session.flush()

    plans = await build_import_group_progress_plans(
        db_session,
        [
            ExecutionItemPlan("new", new_item.id, "New Series", None, None),
            ExecutionItemPlan("duplicate", duplicate_item.id, "Existing Series", None, None),
        ],
        ImportProgressSettings(
            move_to_library=True,
            convert_to_preferred_format=False,
            update_embedded_comicinfo_from_match=False,
        ),
    )

    assert [file_id for file_id, _weight in plans[new_item.id].file_weights] == [
        new_match.id,
        new_preferred_conflict.id,
    ]
    assert [file_id for file_id, _weight in plans[duplicate_item.id].file_weights] == [
        duplicate_selected.id
    ]


@pytest.mark.asyncio
async def test_report_file_progress_persists_durable_stage_boundary() -> None:
    active_calls: list[dict[str, Any]] = []
    live_calls: list[dict[str, Any]] = []

    async def emit_active_file_progress(*args: Any, **kwargs: Any) -> None:
        active_calls.append({"args": args, "kwargs": kwargs})

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_calls.append({"args": args, "kwargs": kwargs})

    callback = build_report_file_progress_callback(
        **_callback_kwargs(
            emit_active_file_progress=emit_active_file_progress,
            emit_live_progress=emit_live_progress,
        )
    )

    await callback(
        imp_file=_file(),
        file_index=1,
        total_files=1,
        stage="finalizing",
        current=1,
        total=1,
        unit="file",
    )

    assert len(active_calls) == 1
    assert live_calls == []
    event = active_calls[0]["kwargs"]["event"]
    assert event.ephemeral_progress is False
    assert event.progress_revision == 12
    assert event.current_series_id == 7
    assert event.current_series_name == "Alpha"
    assert event.current_file_id == 99
    assert event.current_file_name == "Alpha 001.cbz"
    assert event.current_file_stage == "finalizing"
    assert event.current_file_progress_current == 1
    assert event.current_file_progress_total == 1
    assert event.current_file_progress_unit == "file"
    assert event.series_imported == 1
    assert event.series_failed == 2
    assert event.series_found == 3
    assert event.total_files_imported == 4
    assert event.total_files_failed == 5
    assert event.estimated_seconds_remaining is None  # All planned file work is complete.
    assert event.current_item_kind == "file"
    assert event.current_item_stage == "finalizing"
    assert event.current_item_progress_pct == event.current_file_progress_pct
    assert active_calls[0]["args"][0] is not None
    assert active_calls[0]["args"][1] is None
    assert active_calls[0]["kwargs"]["job_id"] == 42


@pytest.mark.asyncio
async def test_report_file_progress_emits_live_only_for_transfer_stage() -> None:
    active_calls: list[dict[str, Any]] = []
    live_calls: list[dict[str, Any]] = []
    revision_state = {"value": 11}

    async def emit_active_file_progress(**kwargs: Any) -> None:
        active_calls.append(kwargs)

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_calls.append({"args": args, "kwargs": kwargs})

    callback = build_report_file_progress_callback(
        **_callback_kwargs(
            revision_state=revision_state,
            emit_active_file_progress=emit_active_file_progress,
            emit_live_progress=emit_live_progress,
        )
    )

    await callback(
        imp_file=_file(),
        file_index=1,
        total_files=1,
        stage="transferring",
        current=1,
        total=4,
        unit="bytes",
    )

    assert active_calls == []
    assert len(live_calls) == 1
    assert live_calls[0]["args"][0].id == 42
    event = live_calls[0]["args"][1]
    assert event.ephemeral_progress is True
    assert event.current_file_stage == "transferring"
    assert live_calls[0]["kwargs"]["revision_state"] is revision_state
    assert live_calls[0]["kwargs"]["started_at"] == datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_report_file_progress_throttles_unchanged_live_progress() -> None:
    active_calls: list[dict[str, Any]] = []
    live_calls: list[dict[str, Any]] = []
    tick_values = iter([10.0, 10.1])

    async def emit_active_file_progress(**kwargs: Any) -> None:
        active_calls.append(kwargs)

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_calls.append({"args": args, "kwargs": kwargs})

    callback = build_report_file_progress_callback(
        **_callback_kwargs(
            monotonic_time=lambda: next(tick_values),
            emit_active_file_progress=emit_active_file_progress,
            emit_live_progress=emit_live_progress,
        )
    )

    for _ in range(2):
        await callback(
            imp_file=_file(),
            file_index=1,
            total_files=1,
            stage="transferring",
            current=1,
            total=4,
            unit="bytes",
        )

    assert active_calls == []
    assert len(live_calls) == 1


@pytest.mark.asyncio
async def test_series_metadata_progress_emitter_persists_durable_event() -> None:
    persisted_events: list[Any] = []
    live_events: list[Any] = []
    revision_state = {"value": 20}
    kwargs = _callback_kwargs(revision_state=revision_state)

    async def emit_progress(
        session: object, job: ImportJob, event: object, callback: object
    ) -> None:
        persisted_events.append(
            {
                "session": session,
                "job": job,
                "event": event,
                "callback": callback,
            }
        )

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_events.append({"args": args, "kwargs": kwargs})

    emitter = build_series_metadata_progress_emitter(
        session=kwargs["session"],
        job_id=42,
        job=kwargs["job"],
        job_started_at=kwargs["job_started_at"],
        work_started_at=kwargs["work_started_at"],
        progress_callback=kwargs["progress_callback"],
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        group_progress_plans=kwargs["group_progress_plans"],
        shared_progress_settings=kwargs["shared_progress_settings"],
        group_progress_weights=kwargs["group_progress_weights"],
        stats=lambda: kwargs["stats"],
        series_found=3,
        revision_state=revision_state,
    )

    await emitter(
        group_index=0,
        total_groups=1,
        series_id=7,
        series_name="Alpha",
        message="Fetching ComicVine metadata for Alpha (review group 1/1)...",
        current_item_stage="metadata_fetch",
        current_item_progress_pct=8,
    )

    assert revision_state["value"] == 21
    assert kwargs["job"].progress_revision == 21
    assert live_events == []
    assert len(persisted_events) == 1
    event = persisted_events[0]["event"]
    assert event.job_id == 42
    assert event.status == ImportJobStatus.IMPORTING
    assert event.phase == "importing"
    assert event.progress_revision == 21
    assert event.current_series_id == 7
    assert event.current_series_name == "Alpha"
    assert event.current_file_id is None
    assert event.current_item_kind == "series"
    assert event.current_item_stage == "metadata_fetch"
    assert event.current_item_progress_pct == 8
    assert event.series_imported == 1
    assert event.series_failed == 2
    assert event.series_found == 3
    assert event.total_files_imported == 4
    assert event.total_files_failed == 5


@pytest.mark.asyncio
async def test_series_metadata_progress_emitter_emits_live_heartbeat() -> None:
    persisted_events: list[Any] = []
    live_events: list[Any] = []
    revision_state = {"value": 20}
    kwargs = _callback_kwargs(revision_state=revision_state)

    async def emit_progress(*args: Any, **kwargs: Any) -> None:
        persisted_events.append({"args": args, "kwargs": kwargs})

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_events.append({"args": args, "kwargs": kwargs})

    emitter = build_series_metadata_progress_emitter(
        session=kwargs["session"],
        job_id=42,
        job=kwargs["job"],
        job_started_at=kwargs["job_started_at"],
        work_started_at=kwargs["work_started_at"],
        progress_callback=kwargs["progress_callback"],
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        group_progress_plans=kwargs["group_progress_plans"],
        shared_progress_settings=kwargs["shared_progress_settings"],
        group_progress_weights=kwargs["group_progress_weights"],
        stats=lambda: kwargs["stats"],
        series_found=3,
        revision_state=revision_state,
    )

    await emitter(
        group_index=0,
        total_groups=1,
        series_id=7,
        series_name="Alpha",
        message="Still fetching ComicVine metadata for Alpha (5s elapsed)...",
        current_item_stage="metadata_fetch_wait",
        current_item_progress_pct=36,
        live_only=True,
    )

    assert persisted_events == []
    assert len(live_events) == 1
    assert live_events[0]["args"][0].id == 42
    event = live_events[0]["args"][1]
    assert event.ephemeral_progress is False
    assert event.current_item_kind == "series"
    assert event.current_item_stage == "metadata_fetch_wait"
    assert event.current_item_progress_pct == 36
    assert live_events[0]["kwargs"]["revision_state"] is revision_state


@pytest.mark.asyncio
async def test_series_metadata_progress_emitter_noops_without_progress_callback() -> None:
    persisted_events: list[Any] = []
    live_events: list[Any] = []
    kwargs = _callback_kwargs(progress_callback=None)

    emitter = build_series_metadata_progress_emitter(
        session=kwargs["session"],
        job_id=42,
        job=kwargs["job"],
        job_started_at=kwargs["job_started_at"],
        work_started_at=kwargs["work_started_at"],
        progress_callback=None,
        emit_progress=lambda *args, **kwargs: persisted_events.append((args, kwargs)),
        emit_live_progress=lambda *args, **kwargs: live_events.append((args, kwargs)),
        group_progress_plans=kwargs["group_progress_plans"],
        shared_progress_settings=kwargs["shared_progress_settings"],
        group_progress_weights=kwargs["group_progress_weights"],
        stats=lambda: kwargs["stats"],
        series_found=3,
        revision_state=kwargs["revision_state"],
    )

    await emitter(
        group_index=0,
        total_groups=1,
        series_id=7,
        series_name="Alpha",
        message="Fetching ComicVine metadata for Alpha...",
        current_item_stage="metadata_fetch",
        current_item_progress_pct=8,
    )

    assert persisted_events == []
    assert live_events == []


@pytest.mark.asyncio
async def test_await_prefetch_with_metadata_progress_emits_heartbeat_and_prepare_events() -> None:
    release_prefetch = asyncio.Event()
    progress_calls: list[dict[str, Any]] = []
    monotonic_values = iter([100.0, 105.0])

    async def prefetch() -> tuple[dict[str, int], list[dict[str, int]]]:
        await release_prefetch.wait()
        return ({"series": 19752}, [{"issue": 1}])

    async def emit_metadata_progress(**kwargs: Any) -> None:
        progress_calls.append(kwargs)

    async def raise_if_cancelled(_session: object, _job_id: int) -> None:
        release_prefetch.set()

    result = await await_prefetch_with_metadata_progress(
        asyncio.create_task(prefetch()),
        group_index=0,
        total_groups=1,
        series_id=7,
        series_name="2000AD",
        session=object(),
        job_id=42,
        raise_if_cancelled=raise_if_cancelled,
        emit_series_metadata_progress=emit_metadata_progress,
        heartbeat_seconds=0.01,
        monotonic_time=lambda: next(monotonic_values),
    )

    assert result == ({"series": 19752}, [{"issue": 1}])
    assert [call["current_item_stage"] for call in progress_calls] == [
        "metadata_fetch",
        "metadata_fetch_wait",
        "series_records",
    ]
    assert progress_calls[0]["message"] == (
        "Fetching ComicVine metadata for 2000AD (review group 1/1)..."
    )
    assert progress_calls[1]["message"] == (
        "Still fetching ComicVine metadata for 2000AD "
        "(5s elapsed)... Large series can take a few minutes."
    )
    assert progress_calls[1]["current_item_progress_pct"] == 36
    assert progress_calls[1]["live_only"] is True
    assert progress_calls[2]["message"] == "Preparing series records for 2000AD..."


@pytest.mark.asyncio
async def test_await_prefetch_with_metadata_progress_cancels_task_on_pause() -> None:
    task_cancelled = False

    async def prefetch() -> tuple[dict[str, int], list[dict[str, int]]]:
        nonlocal task_cancelled
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            task_cancelled = True
            raise
        return ({"series": 19752}, [])

    async def emit_metadata_progress(**_kwargs: Any) -> None:
        return None

    async def raise_if_cancelled(_session: object, _job_id: int) -> None:
        raise JobPausedError

    with pytest.raises(JobPausedError):
        await await_prefetch_with_metadata_progress(
            asyncio.create_task(prefetch()),
            group_index=0,
            total_groups=1,
            series_id=7,
            series_name="2000AD",
            session=object(),
            job_id=42,
            raise_if_cancelled=raise_if_cancelled,
            emit_series_metadata_progress=emit_metadata_progress,
            heartbeat_seconds=0.01,
            monotonic_time=lambda: 1.0,
        )

    assert task_cancelled is True
