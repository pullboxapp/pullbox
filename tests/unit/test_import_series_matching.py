"""Focused tests for live import-series matching behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullbox.services.import_series_matching as import_series_matching
from pullbox.core.exceptions import ImportProviderDegradedError, JobPausedError
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models import Base
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
from pullbox.models.issue import IssueType
from pullbox.services.import_matching import ComicVineMatchEvaluation
from pullbox.services.import_series_matching import run_import_series_matching
from pullbox.services.import_source_metadata import source_metadata_for_matching_series
from pullbox.services.import_workflow_state import phase_progress

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent


def test_mylar_identity_remains_authoritative_when_file_evidence_conflicts() -> None:
    item = ImportedSeries(
        raw_series_name="Firefly",
        raw_year=2018,
        status=ImportSeriesStatus.PENDING,
        cv_id=112340,
        cv_match_method="mylar3_cv_id",
    )
    source_metadata = SourceMetadata(
        original_title="Firefly 007 Variant.cbz",
        series_name="Firefly",
        year=2018,
        comicvine_series_id=112340,
        signals={"comicvine_series_id": MetadataSignal.MYLAR3},
        diagnostics={
            "identity_conflicts": [
                {
                    "field": "comicvine_series_id",
                    "mylar3": 112340,
                    "sidecar": 999999,
                }
            ]
        },
    )

    evaluation = import_series_matching._filesystem_source_identity_evaluation(
        item,
        source_metadata,
        match_threshold=0.88,
    )

    assert evaluation.match is not None
    assert evaluation.match["cv_id"] == 112340
    assert evaluation.match["cv_match_method"] == "mylar3_cv_id"
    assert evaluation.diagnostics["reason"] == "trusted_known_cv_id_unverified"
    assert evaluation.diagnostics["identity_conflicts"] == [
        {
            "field": "comicvine_series_id",
            "mylar3": 112340,
            "sidecar": 999999,
        }
    ]


async def test_series_matching_commits_logs_mid_phase_for_other_sessions(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Alpha",
                    raw_year=2020,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Beta",
                    raw_year=2021,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id

    visible_log_counts: list[int] = []

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> object:
        return object()

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        raw_year = kwargs.get("raw_year")
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 1000 + (1 if raw_name == "Alpha" else 2),
                "cv_title": raw_name,
                "cv_year": raw_year,
                "cv_publisher": "Test Publisher",
                "cv_issue_count": 12,
                "cv_url": f"https://example.com/{raw_name.lower()}",
                "cv_match_score": 0.99,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        session.add(
            ImportJobLog(
                import_job_id=job_id,
                logged_at=datetime.now(UTC),
                level=level.upper(),
                event=event,
                message=message,
                data=details,
            )
        )
        await session.flush()

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        progress_callback,
    ) -> None:
        await progress_callback(event)

    async def maybe_slow_item_delay() -> None:
        return None

    async def progress_callback(event: ImportProgressEvent) -> None:
        if event.current_series != "Alpha":
            return
        async with factory() as verify_session:
            count = await verify_session.scalar(
                select(func.count())
                .select_from(ImportJobLog)
                .where(
                    ImportJobLog.import_job_id == job_id,
                    ImportJobLog.event == "import_series_match_detail",
                )
            )
        visible_log_counts.append(int(count or 0))

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
            progress_callback=progress_callback,
        )

    assert visible_log_counts
    assert any(count >= 1 for count in visible_log_counts)


async def test_series_matching_persists_non_checkpointed_middle_match(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Batman",
                    raw_year=2016,
                    status=ImportSeriesStatus.PENDING,
                    file_count=5,
                ),
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Invincible",
                    raw_year=2003,
                    status=ImportSeriesStatus.PENDING,
                    file_count=5,
                ),
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Saga",
                    raw_year=2012,
                    status=ImportSeriesStatus.PENDING,
                    file_count=5,
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id

    cv_ids = {"Batman": 97508, "Invincible": 17993, "Saga": 42692}

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> object:
        return object()

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        raw_year = kwargs.get("raw_year")
        return ComicVineMatchEvaluation(
            match={
                "cv_id": cv_ids[raw_name],
                "cv_title": raw_name,
                "cv_year": raw_year,
                "cv_publisher": "Test Publisher",
                "cv_issue_count": 12,
                "cv_url": f"https://example.com/{raw_name.lower()}",
                "cv_match_score": 0.99,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={"kind": "series_match"},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        session.add(
            ImportJobLog(
                import_job_id=job_id,
                logged_at=datetime.now(UTC),
                level=level.upper(),
                event=event,
                message=message,
                data=details,
            )
        )
        await session.flush()

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        _event: ImportProgressEvent,
        _progress_callback,
    ) -> None:
        return None

    async def maybe_slow_item_delay() -> None:
        return None

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
        )

    async with factory() as verify_session:
        rows = (
            (
                await verify_session.execute(
                    select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    matched_by_name = {row.raw_series_name: row for row in rows}
    assert set(matched_by_name) == set(cv_ids)
    for raw_name, expected_cv_id in cv_ids.items():
        item = matched_by_name[raw_name]
        assert item.status == ImportSeriesStatus.MATCHED
        assert item.cv_id == expected_cv_id


async def test_series_matching_emits_heartbeat_while_comicvine_evaluation_is_pending(
    async_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr(
        import_series_matching,
        "_MATCH_PROGRESS_HEARTBEAT_SECONDS",
        0.01,
        raising=False,
    )

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add(
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Saga",
                raw_year=2012,
                status=ImportSeriesStatus.PENDING,
                file_count=1,
            )
        )
        await seed_session.commit()
        job_id = job.id

    release_evaluation = asyncio.Event()
    progress_events: list[ImportProgressEvent] = []

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title="Saga", series_name="Saga", year=2012)

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        await release_evaluation.wait()
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 46568,
                "cv_title": str(kwargs["raw_name"]),
                "cv_year": kwargs.get("raw_year"),
                "cv_publisher": "Image",
                "cv_issue_count": 60,
                "cv_url": "https://example.com/saga",
                "cv_match_score": 1.0,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        _session: AsyncSession,
        _job_id: int,
        _level: str,
        _event: str,
        *,
        message: str,
        **_details: Any,
    ) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        progress_callback,
    ) -> None:
        await progress_callback(event)

    async def maybe_slow_item_delay() -> None:
        return None

    async def progress_callback(event: ImportProgressEvent) -> None:
        progress_events.append(event)
        if str(event.message).startswith("Still matching Saga against ComicVine"):
            release_evaluation.set()

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await asyncio.wait_for(
            run_import_series_matching(
                session,
                job,
                metadata_provider=None,
                source_metadata_for_series=source_metadata_for_series,
                evaluate_match=evaluate_match,
                raise_if_cancelled=raise_if_cancelled,
                reclassify_duplicates=reclassify_duplicates,
                recompute_series_counters=recompute_series_counters,
                log_event=log_event,
                emit_progress=emit_progress,
                phase_progress=phase_progress,
                estimate_remaining_seconds=lambda *_args: 42,
                job_stats=lambda _job: {},
                maybe_slow_item_delay=maybe_slow_item_delay,
                progress_callback=progress_callback,
            ),
            timeout=1,
        )

    messages = [str(event.message) for event in progress_events]
    assert "Matching Saga against ComicVine (series 1/1)..." in messages
    assert any(
        message.startswith("Still matching Saga against ComicVine")
        and "Large searches can take a few minutes." in message
        for message in messages
    )
    heartbeat = next(
        event
        for event in progress_events
        if str(event.message).startswith("Still matching Saga against ComicVine")
    )
    assert heartbeat.phase == "matching"
    assert heartbeat.status == ImportJobStatus.MATCHING
    assert heartbeat.current_series == "Saga"
    assert heartbeat.estimated_seconds_remaining == 42


async def test_series_matching_emits_heartbeat_while_volume_rebucket_is_pending(
    async_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr(
        import_series_matching,
        "_MATCH_PROGRESS_HEARTBEAT_SECONDS",
        0.01,
        raising=False,
    )

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action Spider-Man",
            raw_year=2019,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            files_total=1,
            source_folder="/tmp/imports",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path="/tmp/imports/Marvel Action Spider-Man v01 - New Beginning.cbr",
                file_name="Marvel Action Spider-Man v01 - New Beginning (2019) (Digital).cbr",
                file_format="cbr",
                parsed_series="Marvel Action Spider-Man",
                parsed_issue_number=1.0,
                parsed_year=2019,
                status=ImportedFileStatus.PENDING,
                diagnostics={"source_issue_type": IssueType.VOLUME.value},
            )
        )
        await seed_session.commit()
        job_id = job.id

    release_rebucket = asyncio.Event()
    progress_events: list[ImportProgressEvent] = []

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        if raw_name == "Marvel Action Spider-Man":
            return _matched_eval(
                cv_id=124930,
                title="Marvel Action: Spider-Man",
                year=2020,
                issue_count=3,
                raw_name=raw_name,
            )
        await release_rebucket.wait()
        return _matched_eval(
            cv_id=119728,
            title="Marvel Action: Spider-Man: A New Beginning",
            year=2019,
            issue_count=1,
            raw_name=raw_name,
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        _session: AsyncSession,
        _job_id: int,
        _level: str,
        _event: str,
        *,
        message: str,
        **_details: Any,
    ) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        progress_callback,
    ) -> None:
        await progress_callback(event)

    async def maybe_slow_item_delay() -> None:
        return None

    async def progress_callback(event: ImportProgressEvent) -> None:
        progress_events.append(event)
        if str(event.message).startswith("Still checking volume subtitle matches"):
            release_rebucket.set()

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await asyncio.wait_for(
            run_import_series_matching(
                session,
                job,
                metadata_provider=None,
                source_metadata_for_series=source_metadata_for_matching_series,
                evaluate_match=evaluate_match,
                raise_if_cancelled=raise_if_cancelled,
                reclassify_duplicates=reclassify_duplicates,
                recompute_series_counters=recompute_series_counters,
                log_event=log_event,
                emit_progress=emit_progress,
                phase_progress=phase_progress,
                estimate_remaining_seconds=lambda *_args: 42,
                job_stats=lambda _job: {},
                maybe_slow_item_delay=maybe_slow_item_delay,
                progress_callback=progress_callback,
            ),
            timeout=1,
        )

    rebucket_events = [event for event in progress_events if event.current_item_stage == "rebucket"]
    assert rebucket_events
    assert rebucket_events[0].message == (
        "Checking volume subtitle matches for Marvel Action Spider-Man..."
    )
    assert any(
        str(event.message).startswith("Still checking volume subtitle matches")
        for event in rebucket_events
    )
    assert all(
        event.current_item_stage_label == "Checking volume subtitles" for event in rebucket_events
    )


async def test_series_matching_current_item_progress_is_series_local(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Alpha",
                    raw_year=2020,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Beta",
                    raw_year=2021,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id

    progress_events: list[ImportProgressEvent] = []

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title=_item.raw_series_name)

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 1000 + (1 if raw_name == "Alpha" else 2),
                "cv_title": raw_name,
                "cv_year": kwargs.get("raw_year"),
                "cv_publisher": "Test Publisher",
                "cv_issue_count": 1,
                "cv_url": f"https://example.com/{raw_name.lower()}",
                "cv_match_score": 0.99,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        _session: AsyncSession,
        _job_id: int,
        _level: str,
        _event: str,
        *,
        message: str,
        **_details: Any,
    ) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        progress_callback,
    ) -> None:
        await progress_callback(event)

    async def maybe_slow_item_delay() -> None:
        return None

    async def progress_callback(event: ImportProgressEvent) -> None:
        progress_events.append(event)

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
            progress_callback=progress_callback,
        )

    alpha_start = next(
        event
        for event in progress_events
        if event.message == "Matching Alpha against ComicVine (series 1/2)..."
    )
    alpha_done = next(event for event in progress_events if event.message == "Matching 1/2...")
    beta_start = next(
        event
        for event in progress_events
        if event.message == "Matching Beta against ComicVine (series 2/2)..."
    )

    assert alpha_start.progress < beta_start.progress
    assert alpha_start.current_item_progress_pct == 0
    assert alpha_done.current_item_progress_pct == 100
    assert beta_start.current_item_progress_pct == 0


async def test_series_matching_eta_uses_total_series_work(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC) - timedelta(minutes=2),
            match_started_at=datetime.now(UTC) - timedelta(seconds=20),
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Alpha",
                    raw_year=2020,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name="Beta",
                    raw_year=2021,
                    status=ImportSeriesStatus.PENDING,
                    file_count=1,
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id
        expected_started_at = job.match_started_at

    progress_events: list[ImportProgressEvent] = []
    eta_calls: list[dict[str, Any]] = []

    async def source_metadata_for_series(
        _session: AsyncSession,
        item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title=item.raw_series_name)

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 1000,
                "cv_title": str(kwargs["raw_name"]),
                "cv_year": kwargs.get("raw_year"),
                "cv_publisher": "Test Publisher",
                "cv_issue_count": 1,
                "cv_url": f"https://example.com/{str(kwargs['raw_name']).lower()}",
                "cv_match_score": 1.0,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        progress_callback,
    ) -> None:
        await progress_callback(event)

    async def progress_callback(event: ImportProgressEvent) -> None:
        progress_events.append(event)

    async def maybe_slow_item_delay() -> None:
        return None

    def estimate_remaining_work_seconds(
        started_at: datetime | None,
        *,
        completed_units: int | float,
        total_units: int | float,
        current_unit_progress_pct: int | float | None = None,
    ) -> int:
        eta_calls.append(
            {
                "started_at": started_at,
                "completed_units": completed_units,
                "total_units": total_units,
                "current_unit_progress_pct": current_unit_progress_pct,
            }
        )
        return 888

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: 12,
            estimate_remaining_work_seconds=estimate_remaining_work_seconds,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
            progress_callback=progress_callback,
        )

    alpha_start = next(
        event
        for event in progress_events
        if event.message == "Matching Alpha against ComicVine (series 1/2)..."
    )
    alpha_done = next(event for event in progress_events if event.message == "Matching 1/2...")

    assert alpha_start.estimated_seconds_remaining == 888
    assert alpha_done.estimated_seconds_remaining == 888
    assert eta_calls[0]["started_at"] == expected_started_at
    assert eta_calls[0]["completed_units"] > 0
    assert eta_calls[0]["total_units"] > 2
    assert eta_calls[0]["current_unit_progress_pct"] is None
    assert any(
        call["started_at"] == expected_started_at
        and call["completed_units"] > eta_calls[0]["completed_units"]
        and call["total_units"] == eta_calls[0]["total_units"]
        and call["current_unit_progress_pct"] is None
        for call in eta_calls
    )


async def test_series_matching_detail_log_lock_does_not_rollback_match(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Predator: Bloodshed",
            raw_year=2026,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
        )
        seed_session.add(item)
        await seed_session.commit()
        job_id = job.id
        item_id = item.id

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> object:
        return object()

    async def evaluate_match(**_kwargs: Any) -> ComicVineMatchEvaluation:
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 170670,
                "cv_title": "Predator: Bloodshed",
                "cv_year": 2026,
                "cv_publisher": "Marvel",
                "cv_issue_count": 4,
                "cv_url": "https://example.com/predator-bloodshed",
                "cv_match_score": 1.0,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={
                "kind": "series_match",
                "reason": "matched",
                "selected_candidate": {"cv_id": 170670},
            },
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        if event == "import_series_match_detail":
            raise OperationalError(
                "INSERT INTO import_job_logs",
                {},
                Exception("database is locked"),
            )
        session.add(
            ImportJobLog(
                import_job_id=job_id,
                logged_at=datetime.now(UTC),
                level=level.upper(),
                event=event,
                message=message,
                data=details,
            )
        )
        await session.flush()

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        _event: ImportProgressEvent,
        _progress_callback,
    ) -> None:
        return None

    async def maybe_slow_item_delay() -> None:
        return None

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
        )
        await session.commit()

    async with factory() as verify_session:
        item = await verify_session.get(ImportedSeries, item_id)
        logs = (
            (
                await verify_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    assert item is not None
    assert item.status == ImportSeriesStatus.MATCHED
    assert item.cv_id == 170670
    assert item.diagnostics["kind"] == "series_match"
    assert item.diagnostics["selected_candidate"]["cv_id"] == 170670
    assert not any(log.event == "import_series_match_detail" for log in logs)
    assert any(log.event == "import_matching_completed" for log in logs)


async def test_series_matching_retries_with_deferred_archive_metadata(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path="/tmp/imports/Batman 001.cbz",
                file_name="Batman 001.cbz",
                file_format="cbz",
                parsed_series="Batman",
                parsed_issue_number=1.0,
                parsed_year=2016,
                diagnostics={
                    "source_metadata": {
                        "archive_metadata_loaded": False,
                        "archive_metadata_deferred": True,
                    }
                },
            )
        )
        await seed_session.commit()
        job_id = job.id

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title="Batman", series_name="Weak Batman", year=2016)

    async def load_deferred_source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title="Batman", series_name="Batman", year=2016)

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        metadata = kwargs["source_metadata"]
        if getattr(metadata, "series_name", "") != "Batman":
            return ComicVineMatchEvaluation(match=None, diagnostics={"kind": "series_no_match"})
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 97508,
                "cv_title": "Batman",
                "cv_year": 2016,
                "cv_publisher": "DC Comics",
                "cv_issue_count": 120,
                "cv_url": "https://example.com/batman",
                "cv_match_score": 0.99,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        _session: AsyncSession,
        _job_id: int,
        _level: str,
        _event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        _event: ImportProgressEvent,
        _progress_callback,
    ) -> None:
        return None

    async def maybe_slow_item_delay() -> None:
        return None

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_series,
            load_deferred_source_metadata_for_series=load_deferred_source_metadata_for_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
        )

    async with factory() as verify_session:
        item = await verify_session.scalar(
            select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
        )
    assert item is not None
    assert item.status == ImportSeriesStatus.MATCHED
    assert item.cv_id == 97508


async def test_series_matching_pauses_instead_of_poisoning_provider_failures(async_engine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Free for All",
            raw_year=2025,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path="/tmp/imports/Free for All (2025) #001.pdf",
                file_name="Free for All (2025) #001.pdf",
                file_format="pdf",
                parsed_series="Free for All",
                parsed_issue_number=1.0,
                parsed_year=2025,
            )
        )
        await seed_session.commit()
        job_id = job.id
        item_id = item.id

    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title="Free for All", series_name="Free for All", year=2025)

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raise ImportProviderDegradedError(
            provider="comicvine",
            query=str(kwargs["raw_name"]),
            year=kwargs.get("raw_year"),
            attempts=3,
            last_error="Request timed out: /search/",
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        session.add(
            ImportJobLog(
                import_job_id=job_id,
                logged_at=datetime.now(UTC),
                level=level.upper(),
                event=event,
                message=message,
                data=details,
            )
        )
        await session.flush()

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        _event: ImportProgressEvent,
        _progress_callback,
    ) -> None:
        return None

    async def maybe_slow_item_delay() -> None:
        return None

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        with pytest.raises(JobPausedError):
            await run_import_series_matching(
                session,
                job,
                metadata_provider=None,
                source_metadata_for_series=source_metadata_for_series,
                evaluate_match=evaluate_match,
                raise_if_cancelled=raise_if_cancelled,
                reclassify_duplicates=reclassify_duplicates,
                recompute_series_counters=recompute_series_counters,
                log_event=log_event,
                emit_progress=emit_progress,
                phase_progress=phase_progress,
                estimate_remaining_seconds=lambda *_args: None,
                job_stats=lambda _job: {},
                maybe_slow_item_delay=maybe_slow_item_delay,
            )

    async with factory() as verify_session:
        job = await verify_session.get(ImportJob, job_id)
        item = await verify_session.get(ImportedSeries, item_id)
        logs = (
            (
                await verify_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    assert job is not None
    assert item is not None
    assert job.status == ImportJobStatus.MATCHING
    assert "ComicVine timed out while matching 1 series" in str(job.error_message)
    assert item.status == ImportSeriesStatus.PENDING
    assert item.diagnostics["kind"] == "series_provider_error"
    assert any(log.event == "import_matching_provider_degraded" for log in logs)


async def test_collection_volume_subtitles_rebucket_to_separate_one_issue_series(
    async_engine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action Spider-Man",
            raw_year=2019,
            status=ImportSeriesStatus.PENDING,
            file_count=2,
            files_total=2,
            sample_paths=[],
            source_folder="/tmp/imports",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Marvel Action Spider-Man v01 - New Beginning.cbr",
                    file_name=("Marvel Action Spider-Man v01 - New Beginning (2019) (Digital).cbr"),
                    file_format="cbr",
                    parsed_series="Marvel Action Spider-Man",
                    parsed_issue_number=1.0,
                    parsed_year=2019,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Marvel Action Spider-Man v02 - Spider-Chase.cbr",
                    file_name=("Marvel Action Spider-Man v02 - Spider-Chase (2019) (Digital).cbr"),
                    file_format="cbr",
                    parsed_series="Marvel Action Spider-Man",
                    parsed_issue_number=2.0,
                    parsed_year=2019,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id
        parent_id = item.id

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        if raw_name == "Marvel Action Spider-Man":
            return _matched_eval(
                cv_id=124930,
                title="Marvel Action: Spider-Man",
                year=2020,
                issue_count=3,
                raw_name=raw_name,
            )
        if "New Beginning" in raw_name:
            return _matched_eval(
                cv_id=119728,
                title="Marvel Action: Spider-Man: A New Beginning",
                year=2019,
                issue_count=1,
                raw_name=raw_name,
            )
        if "Spider-Chase" in raw_name:
            return _matched_eval(
                cv_id=122410,
                title="Marvel Action: Spider-Man: Spider-Chase",
                year=2019,
                issue_count=1,
                raw_name=raw_name,
            )
        return ComicVineMatchEvaluation(match=None, diagnostics={"kind": "series_no_match"})

    await _run_matching_for_test(
        factory,
        job_id,
        evaluate_match=evaluate_match,
    )

    async with factory() as verify_session:
        rows = (
            (
                await verify_session.execute(
                    select(ImportedSeries)
                    .where(ImportedSeries.import_job_id == job_id)
                    .order_by(ImportedSeries.cv_id.asc().nulls_last())
                )
            )
            .scalars()
            .all()
        )
        files = (
            (
                await verify_session.execute(
                    select(ImportedFile).where(ImportedFile.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    rows_by_cv_id = {row.cv_id: row for row in rows if row.cv_id is not None}
    assert rows_by_cv_id[119728].raw_series_name == "Marvel Action: Spider-Man: A New Beginning"
    assert rows_by_cv_id[122410].raw_series_name == "Marvel Action: Spider-Man: Spider-Chase"
    assert rows_by_cv_id[119728].files_total == 1
    assert rows_by_cv_id[122410].files_total == 1
    assert rows_by_cv_id[119728].diagnostics["split_reason"] == "volume_subtitle_series_match"
    assert rows_by_cv_id[122410].diagnostics["split_reason"] == "volume_subtitle_series_match"

    assert len(rows) == 2
    assert all(row.id != parent_id for row in rows)
    assert {file.import_series_id for file in files} == {
        rows_by_cv_id[119728].id,
        rows_by_cv_id[122410].id,
    }


async def test_collection_volume_rebucket_preserves_each_file_year_when_cv_year_missing(
    async_engine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action Spider-Man",
            raw_year=2019,
            status=ImportSeriesStatus.PENDING,
            file_count=2,
            files_total=2,
            sample_paths=[],
            source_folder="/tmp/imports",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Marvel Action Spider-Man v01 - New Beginning.cbr",
                    file_name="Marvel Action Spider-Man v01 - New Beginning (2019) (Digital).cbr",
                    file_format="cbr",
                    parsed_series="Marvel Action Spider-Man",
                    parsed_issue_number=1.0,
                    parsed_year=2019,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Marvel Action Spider-Man v02 - Spider-Chase.cbr",
                    file_name="Marvel Action Spider-Man v02 - Spider-Chase (2020) (Digital).cbr",
                    file_format="cbr",
                    parsed_series="Marvel Action Spider-Man",
                    parsed_issue_number=2.0,
                    parsed_year=2020,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        if raw_name == "Marvel Action Spider-Man":
            return _matched_eval(
                cv_id=124930,
                title="Marvel Action: Spider-Man",
                year=2020,
                issue_count=3,
                raw_name=raw_name,
            )
        if "New Beginning" in raw_name:
            return _matched_eval(
                cv_id=119728,
                title="Marvel Action: Spider-Man: A New Beginning",
                year=2019,
                issue_count=1,
                raw_name=raw_name,
            )
        if "Spider-Chase" in raw_name:
            evaluation = _matched_eval(
                cv_id=122410,
                title="Marvel Action: Spider-Man: Spider-Chase",
                year=2020,
                issue_count=1,
                raw_name=raw_name,
            )
            assert evaluation.match is not None
            evaluation.match["cv_year"] = None
            return evaluation
        return ComicVineMatchEvaluation(match=None, diagnostics={"kind": "series_no_match"})

    await _run_matching_for_test(
        factory,
        job_id,
        evaluate_match=evaluate_match,
    )

    async with factory() as verify_session:
        split_row = await verify_session.scalar(
            select(ImportedSeries).where(ImportedSeries.cv_id == 122410)
        )

    assert split_row is not None
    assert split_row.cv_year is None
    assert split_row.raw_year == 2020


async def test_collection_volume_rebucket_does_not_block_persistent_cache_writes(
    tmp_path,
) -> None:
    db_path = tmp_path / "rebucket-lock.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 0.05},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action Spider-Man",
            raw_year=2019,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            files_total=1,
            source_folder="/tmp/imports",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path="/tmp/imports/Marvel Action Spider-Man v01 - New Beginning.cbr",
                file_name="Marvel Action Spider-Man v01 - New Beginning (2019) (Digital).cbr",
                file_format="cbr",
                parsed_series="Marvel Action Spider-Man",
                parsed_issue_number=1.0,
                parsed_year=2019,
                status=ImportedFileStatus.PENDING,
                diagnostics={"source_issue_type": IssueType.VOLUME.value},
            )
        )
        await seed_session.commit()
        job_id = job.id

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        if raw_name == "Marvel Action Spider-Man":
            return _matched_eval(
                cv_id=124930,
                title="Marvel Action: Spider-Man",
                year=2020,
                issue_count=3,
                raw_name=raw_name,
            )

        # This mirrors the persistent ComicVine cache: it writes through a
        # separate session while the import session is mid-match.
        async with factory() as cache_like_session:
            cache_like_session.add(
                ImportJobLog(
                    import_job_id=job_id,
                    logged_at=datetime.now(UTC),
                    level="DEBUG",
                    event="cache_like_rebucket_write",
                    message="cache-like writer committed during rebucket",
                    data={},
                )
            )
            await cache_like_session.commit()

        return _matched_eval(
            cv_id=119728,
            title="Marvel Action: Spider-Man: A New Beginning",
            year=2019,
            issue_count=1,
            raw_name=raw_name,
        )

    try:
        await _run_matching_for_test(
            factory,
            job_id,
            evaluate_match=evaluate_match,
        )

        async with factory() as verify_session:
            cache_log_count = await verify_session.scalar(
                select(func.count(ImportJobLog.id)).where(
                    ImportJobLog.event == "cache_like_rebucket_write"
                )
            )
            split_row = await verify_session.scalar(
                select(ImportedSeries).where(ImportedSeries.cv_id == 119728)
            )
    finally:
        await engine.dispose()

    assert cache_log_count == 1
    assert split_row is not None
    assert split_row.cv_match_method == "volume_subtitle_series_match"


async def test_trusted_mylar_series_match_skips_volume_subtitle_rebucket(
    async_engine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports/mylar3.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Alien By Shalvey & Broccardo",
            raw_year=2024,
            status=ImportSeriesStatus.PENDING,
            file_count=2,
            files_total=2,
            source_folder="/tmp/imports/Alien By Shalvey & Broccardo",
            cv_id=154680,
            cv_match_method="mylar3_cv_id",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Alien by Shalvey & Broccardo v01 - Thaw.cbz",
                    file_name=(
                        "Alien by Shalvey & Broccardo v01 - Thaw "
                        "(2024) (Digital) (dekabro-Empire).cbz"
                    ),
                    file_format="cbz",
                    parsed_series="Alien by Shalvey & Broccardo",
                    parsed_issue_number=1.0,
                    parsed_year=2024,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Alien by Shalvey & Broccardo v02 - Descendant.cbz",
                    file_name=(
                        "Alien by Shalvey & Broccardo v02 - Descendant "
                        "(2024) (Digital) (Kileko-Empire).cbz"
                    ),
                    file_format="cbz",
                    parsed_series="Alien by Shalvey & Broccardo",
                    parsed_issue_number=2.0,
                    parsed_year=2024,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id
        parent_id = item.id

    seen_raw_names: list[str] = []

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        seen_raw_names.append(raw_name)
        assert kwargs["mylar3_cv_id"] == 154680
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 154680,
                "cv_title": "Alien By Shalvey & Broccardo",
                "cv_year": 2024,
                "cv_publisher": "Marvel",
                "cv_issue_count": 2,
                "cv_url": "https://example.com/154680",
                "cv_match_score": 1.0,
                "cv_match_method": "mylar3_cv_id",
            },
            diagnostics={"kind": "series_match", "reason": "trusted_known_cv_id_unverified"},
        )

    await _run_matching_for_test(
        factory,
        job_id,
        evaluate_match=evaluate_match,
    )

    async with factory() as verify_session:
        rows = (
            (
                await verify_session.execute(
                    select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    assert seen_raw_names == ["Alien By Shalvey & Broccardo"]
    assert len(rows) == 1
    assert rows[0].id == parent_id
    assert rows[0].cv_match_method == "mylar3_cv_id"
    assert rows[0].files_total == 2


async def test_collection_volume_subtitles_stay_grouped_without_one_issue_series_match(
    async_engine,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Immortal Thor",
            raw_year=2024,
            status=ImportSeriesStatus.PENDING,
            file_count=2,
            files_total=2,
            source_folder="/tmp/imports",
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Immortal Thor v01 - All Weather Turns to Storm.cbz",
                    file_name=(
                        "Immortal Thor v01 - All Weather Turns to Storm (2024) (Digital).cbz"
                    ),
                    file_format="cbz",
                    parsed_series="Immortal Thor",
                    parsed_issue_number=1.0,
                    parsed_year=2024,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/imports/Immortal Thor v02 - All Trials Are One.cbz",
                    file_name="Immortal Thor v02 - All Trials Are One (2024) (Digital).cbz",
                    file_format="cbz",
                    parsed_series="Immortal Thor",
                    parsed_issue_number=2.0,
                    parsed_year=2024,
                    status=ImportedFileStatus.PENDING,
                    diagnostics={"source_issue_type": IssueType.VOLUME.value},
                ),
            ]
        )
        await seed_session.commit()
        job_id = job.id
        parent_id = item.id

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        return _matched_eval(
            cv_id=157225,
            title="Immortal Thor",
            year=2024,
            issue_count=5,
            raw_name=raw_name,
        )

    await _run_matching_for_test(
        factory,
        job_id,
        evaluate_match=evaluate_match,
    )

    async with factory() as verify_session:
        rows = (
            (
                await verify_session.execute(
                    select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )
        files = (
            (
                await verify_session.execute(
                    select(ImportedFile).where(ImportedFile.import_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].id == parent_id
    assert rows[0].status == ImportSeriesStatus.MATCHED
    assert rows[0].cv_id == 157225
    assert rows[0].files_total == 2
    assert {file.import_series_id for file in files} == {parent_id}


def _matched_eval(
    *,
    cv_id: int,
    title: str,
    year: int,
    issue_count: int,
    raw_name: str,
) -> ComicVineMatchEvaluation:
    return ComicVineMatchEvaluation(
        match={
            "cv_id": cv_id,
            "cv_title": title,
            "cv_year": year,
            "cv_publisher": "IDW Publishing" if "Marvel Action" in title else "Marvel",
            "cv_issue_count": issue_count,
            "cv_url": f"https://example.com/{cv_id}",
            "cv_match_score": 1.0,
            "cv_match_method": "exact_title_year",
        },
        diagnostics={
            "kind": "series_match",
            "reason": "matched",
            "raw_name": raw_name,
            "selected_candidate": {
                "cv_id": cv_id,
                "title": title,
                "score": 1.0,
                "match_type": "exact",
            },
        },
    )


async def _run_matching_for_test(
    factory: async_sessionmaker,
    job_id: int,
    *,
    evaluate_match,
) -> None:
    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async def reclassify_duplicates(_session: AsyncSession, _job: ImportJob) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        session.add(
            ImportJobLog(
                import_job_id=job_id,
                logged_at=datetime.now(UTC),
                level=level.upper(),
                event=event,
                message=message,
                data=details,
            )
        )
        await session.flush()

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        _event: ImportProgressEvent,
        _progress_callback,
    ) -> None:
        return None

    async def maybe_slow_item_delay() -> None:
        return None

    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job is not None
        await run_import_series_matching(
            session,
            job,
            metadata_provider=None,
            source_metadata_for_series=source_metadata_for_matching_series,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            reclassify_duplicates=reclassify_duplicates,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            maybe_slow_item_delay=maybe_slow_item_delay,
        )
        await session.commit()
