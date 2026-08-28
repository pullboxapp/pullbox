"""Tests for import file-matching progress helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from pullbox.core.exceptions import JobPausedError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_file_matching_progress import (
    build_file_matching_progress_emitter,
    load_file_match_target_index_with_progress,
)
from pullbox.services.import_progress_runtime import (
    ScanReviewFileMatchProfile,
    ScanReviewSeriesMatchProfile,
    scan_review_completed_weight,
    scan_review_progress_plan,
)

if TYPE_CHECKING:
    from pullbox.schemas.import_job import ImportProgressEvent


def _job() -> ImportJob:
    return ImportJob(
        id=42,
        source_path="/tmp/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FILE_MATCHING,
        scan_started_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
        match_completed_at=datetime(2026, 6, 9, 12, 30, tzinfo=UTC),
        progress_revision=10,
    )


def _series() -> ImportedSeries:
    return ImportedSeries(
        id=7,
        import_job_id=42,
        raw_series_name="Batman",
        status=ImportSeriesStatus.MATCHED,
    )


@pytest.mark.asyncio
async def test_file_matching_progress_emitter_persists_durable_event() -> None:
    persisted_events: list[dict[str, Any]] = []
    live_events: list[dict[str, Any]] = []
    revision_state = {"value": 10}

    async def emit_progress(
        session: object,
        job: ImportJob,
        event: ImportProgressEvent,
        callback: object,
    ) -> None:
        event.progress_revision = 12
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

    session = object()
    progress_callback = object()
    phase_args: list[tuple[int, int, int, int]] = []
    emitter = build_file_matching_progress_emitter(
        session=session,
        job=_job(),
        progress_callback=progress_callback,
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        phase_progress=lambda start, end, completed, total: (
            phase_args.append((start, end, completed, total)) or 86
        ),
        estimate_remaining_seconds=lambda _started_at, _progress: 321,
        job_stats=lambda _job: {"series_found": 3, "total_files_found": 4},
        total_file_phase_units=5,
        revision_state=revision_state,
    )

    await emitter(
        _series(),
        2,
        message="Matching files to issues for Batman (1 file)...",
        current_item_progress_pct=40,
    )

    assert live_events == []
    assert revision_state["value"] == 12
    assert phase_args == [(80, 99, 2, 5)]
    assert len(persisted_events) == 1
    event = persisted_events[0]["event"]
    assert event.job_id == 42
    assert event.status == ImportJobStatus.FILE_MATCHING
    assert event.phase == "file_matching"
    assert event.progress == 86
    assert event.message == "Matching files to issues for Batman (1 file)..."
    assert event.current_series == "Batman"
    assert event.current_series_status == ImportSeriesStatus.MATCHED
    assert event.estimated_seconds_remaining == 321
    assert event.current_item_kind == "series"
    assert event.current_item_stage == "file_matching"
    assert event.current_item_progress_pct == 40
    assert event.series_found == 3
    assert event.total_files_found == 4


@pytest.mark.asyncio
async def test_file_matching_progress_eta_uses_total_work_units() -> None:
    persisted_events: list[ImportProgressEvent] = []
    work_eta_calls: list[dict[str, Any]] = []
    job = _job()
    job.match_completed_at = datetime.now(UTC) - timedelta(seconds=20)

    async def emit_progress(
        _session: object,
        _job: ImportJob,
        event: ImportProgressEvent,
        _callback: object,
    ) -> None:
        persisted_events.append(event)

    async def emit_live_progress(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("expected durable progress")

    def estimate_remaining_work_seconds(
        started_at: datetime | None,
        *,
        completed_units: int | float,
        total_units: int | float,
        current_unit_progress_pct: int | float | None = None,
    ) -> int:
        work_eta_calls.append(
            {
                "started_at": started_at,
                "completed_units": completed_units,
                "total_units": total_units,
                "current_unit_progress_pct": current_unit_progress_pct,
            }
        )
        return 777

    emitter = build_file_matching_progress_emitter(
        session=object(),
        job=job,
        progress_callback=object(),
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        phase_progress=lambda _start, _end, _completed, _total: 91,
        estimate_remaining_seconds=lambda *_args: 12,
        estimate_remaining_work_seconds=estimate_remaining_work_seconds,
        job_stats=lambda _job: {},
        total_file_phase_units=10,
        revision_state={"value": 10},
    )

    await emitter(
        _series(),
        3,
        message="Matched file 2/4 for Batman",
        current_item_progress_pct=50,
    )

    assert len(persisted_events) == 1
    assert persisted_events[0].estimated_seconds_remaining == 777
    assert work_eta_calls == [
        {
            "started_at": job.match_completed_at,
            "completed_units": 3,
            "total_units": 10,
            "current_unit_progress_pct": 50,
        }
    ]


@pytest.mark.asyncio
async def test_file_matching_display_progress_does_not_precredit_next_work_unit() -> None:
    persisted_events: list[ImportProgressEvent] = []
    work_eta_calls: list[dict[str, Any]] = []
    plan = scan_review_progress_plan(
        analysis_series_count=1,
        series_match_profiles=[ScanReviewSeriesMatchProfile(file_count=2)],
        file_match_profiles=[ScanReviewFileMatchProfile(file_count=2, issue_count=12)],
    )

    async def emit_progress(
        _session: object,
        _job: ImportJob,
        event: ImportProgressEvent,
        _callback: object,
    ) -> None:
        persisted_events.append(event)

    def estimate_remaining_work_seconds(
        _started_at: datetime | None,
        *,
        completed_units: int | float,
        total_units: int | float,
        current_unit_progress_pct: int | float | None = None,
    ) -> int:
        work_eta_calls.append(
            {
                "completed_units": completed_units,
                "total_units": total_units,
                "current_unit_progress_pct": current_unit_progress_pct,
            }
        )
        return 123

    emitter = build_file_matching_progress_emitter(
        session=object(),
        job=_job(),
        progress_callback=object(),
        emit_progress=emit_progress,
        emit_live_progress=lambda *_args, **_kwargs: None,
        phase_progress=lambda *_args: 91,
        estimate_remaining_seconds=lambda *_args: 12,
        estimate_remaining_work_seconds=estimate_remaining_work_seconds,
        job_stats=lambda _job: {},
        total_file_phase_units=3,
        revision_state={"value": 10},
        scan_review_plan=plan,
    )

    await emitter(
        _series(),
        2,
        message="Matched file 1/2 for Batman",
        current_item_progress_pct=75,
        current_work_unit_progress_pct=0,
    )

    expected_completed_weight = scan_review_completed_weight(
        plan,
        phase="file_matching",
        completed_items=2,
        current_item_progress_pct=0,
    )
    assert persisted_events[0].current_item_progress_pct == 75
    assert work_eta_calls[0]["completed_units"] == expected_completed_weight


@pytest.mark.asyncio
async def test_file_matching_progress_emitter_sends_live_event() -> None:
    persisted_events: list[dict[str, Any]] = []
    live_events: list[dict[str, Any]] = []
    revision_state = {"value": 10}
    job = _job()

    async def emit_progress(*args: Any, **kwargs: Any) -> None:
        persisted_events.append({"args": args, "kwargs": kwargs})

    async def emit_live_progress(*args: Any, **kwargs: Any) -> None:
        live_events.append({"args": args, "kwargs": kwargs})

    emitter = build_file_matching_progress_emitter(
        session=object(),
        job=job,
        progress_callback=object(),
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        phase_progress=lambda _start, _end, _completed, _total: 88,
        estimate_remaining_seconds=lambda _started_at, _progress: None,
        job_stats=lambda _job: {},
        total_file_phase_units=5,
        revision_state=revision_state,
    )

    await emitter(
        _series(),
        2,
        message="Still loading issue targets for Batman (5s elapsed)...",
        current_item_progress_pct=30,
        live_only=True,
    )

    assert persisted_events == []
    assert len(live_events) == 1
    assert live_events[0]["args"][0] is job
    event = live_events[0]["args"][1]
    assert event.current_series == "Batman"
    assert event.current_item_progress_pct == 30
    assert live_events[0]["kwargs"]["revision_state"] is revision_state
    assert live_events[0]["kwargs"]["started_at"] == datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_file_matching_progress_emitter_noops_without_callback() -> None:
    persisted_events: list[object] = []
    live_events: list[object] = []
    emitter = build_file_matching_progress_emitter(
        session=object(),
        job=_job(),
        progress_callback=None,
        emit_progress=lambda *args, **kwargs: persisted_events.append((args, kwargs)),
        emit_live_progress=lambda *args, **kwargs: live_events.append((args, kwargs)),
        phase_progress=lambda _start, _end, _completed, _total: 88,
        estimate_remaining_seconds=lambda _started_at, _progress: None,
        job_stats=lambda _job: {},
        total_file_phase_units=5,
        revision_state={"value": 10},
    )

    await emitter(
        _series(),
        2,
        message="Matching files to issues for Batman...",
    )

    assert persisted_events == []
    assert live_events == []


@pytest.mark.asyncio
async def test_load_file_match_target_index_with_progress_bypasses_without_callback() -> None:
    target_index = object()
    load_calls: list[dict[str, Any]] = []

    async def load_file_match_target_index(*args: Any, **kwargs: Any) -> object:
        load_calls.append({"args": args, "kwargs": kwargs})
        return target_index

    async def emit_file_matching_progress(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("progress should not be emitted")

    result = await load_file_match_target_index_with_progress(
        session=object(),
        job_id=42,
        item=_series(),
        files_to_match=[ImportedFile(id=101, file_name="Batman 001.cbz")],
        duplicate_series=False,
        metadata_provider=object(),
        series_idx=0,
        total_series=1,
        completed_units=0,
        progress_callback=None,
        emit_file_matching_progress=emit_file_matching_progress,
        raise_if_cancelled=lambda *_args: None,
        load_file_match_target_index=load_file_match_target_index,
    )

    assert result is target_index
    assert len(load_calls) == 1
    assert load_calls[0]["kwargs"]["files"][0].file_name == "Batman 001.cbz"


@pytest.mark.asyncio
async def test_load_file_match_target_index_with_progress_emits_heartbeat() -> None:
    target_index = object()
    events: list[dict[str, Any]] = []
    release_provider = asyncio.Event()

    async def load_file_match_target_index(*args: Any, **kwargs: Any) -> object:
        await release_provider.wait()
        return target_index

    async def emit_file_matching_progress(*args: Any, **kwargs: Any) -> None:
        events.append({"args": args, "kwargs": kwargs})
        if kwargs.get("live_only"):
            release_provider.set()

    async def raise_if_cancelled(*_args: Any) -> None:
        return None

    series = _series()
    result = await load_file_match_target_index_with_progress(
        session=object(),
        job_id=42,
        item=series,
        files_to_match=[],
        duplicate_series=False,
        metadata_provider=object(),
        series_idx=0,
        total_series=1,
        completed_units=3,
        progress_callback=object(),
        emit_file_matching_progress=emit_file_matching_progress,
        raise_if_cancelled=raise_if_cancelled,
        load_file_match_target_index=load_file_match_target_index,
        heartbeat_seconds=0.01,
    )

    assert result is target_index
    assert events[0]["args"][:2] == (series, 3)
    assert events[0]["kwargs"] == {
        "message": "Loading issue targets for Batman (series 1/1)...",
        "current_item_stage": "file_matching",
        "current_item_progress_pct": 5,
        "current_work_unit_progress_pct": 0,
    }
    assert events[1]["kwargs"]["message"].startswith(
        "Still loading issue targets for Batman (1s elapsed)..."
    )
    assert events[1]["kwargs"]["current_item_progress_pct"] == 15
    assert events[1]["kwargs"]["current_work_unit_progress_pct"] == 15
    assert events[1]["kwargs"]["live_only"] is True


@pytest.mark.asyncio
async def test_load_file_match_target_index_emits_start_for_existing_series() -> None:
    target_index = object()
    events: list[dict[str, Any]] = []
    series = _series()
    series.series_id = 99

    async def load_file_match_target_index(*_args: Any, **_kwargs: Any) -> object:
        return target_index

    async def emit_file_matching_progress(*args: Any, **kwargs: Any) -> None:
        events.append({"args": args, "kwargs": kwargs})

    result = await load_file_match_target_index_with_progress(
        session=object(),
        job_id=42,
        item=series,
        files_to_match=[],
        duplicate_series=False,
        metadata_provider=None,
        series_idx=2,
        total_series=4,
        completed_units=7,
        progress_callback=object(),
        emit_file_matching_progress=emit_file_matching_progress,
        raise_if_cancelled=lambda *_args: None,
        load_file_match_target_index=load_file_match_target_index,
    )

    assert result is target_index
    assert events == [
        {
            "args": (series, 7),
            "kwargs": {
                "message": "Loading issue targets for Batman (series 3/4)...",
                "current_item_stage": "file_matching",
                "current_item_progress_pct": 5,
                "current_work_unit_progress_pct": 0,
            },
        }
    ]


@pytest.mark.asyncio
async def test_load_file_match_target_index_with_progress_cancels_task_on_pause() -> None:
    cancelled = asyncio.Event()

    async def load_file_match_target_index(*args: Any, **kwargs: Any) -> object:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def emit_file_matching_progress(*args: Any, **kwargs: Any) -> None:
        return None

    async def raise_if_cancelled(*_args: Any) -> None:
        raise JobPausedError("paused")

    with pytest.raises(JobPausedError):
        await load_file_match_target_index_with_progress(
            session=object(),
            job_id=42,
            item=_series(),
            files_to_match=[],
            duplicate_series=False,
            metadata_provider=object(),
            series_idx=0,
            total_series=1,
            completed_units=3,
            progress_callback=object(),
            emit_file_matching_progress=emit_file_matching_progress,
            raise_if_cancelled=raise_if_cancelled,
            load_file_match_target_index=load_file_match_target_index,
            heartbeat_seconds=0.01,
        )

    assert cancelled.is_set()
