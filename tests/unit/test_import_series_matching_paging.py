"""Bounded paging tests for import series matching."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import pullbox.services.import_series_matching as import_series_matching
from pullbox.core.source_metadata import SourceMetadata
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_matching import ComicVineMatchEvaluation
from pullbox.services.import_workflow_state import phase_progress

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import pytest
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent


async def _run_matching(
    session: AsyncSession,
    job: ImportJob,
    *,
    evaluate_match: Callable[..., Coroutine[Any, Any, ComicVineMatchEvaluation]],
    raise_if_cancelled: Callable[[AsyncSession, int], Awaitable[None]],
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    item_page_size: int = 100,
    file_page_size: int = 250,
    profile_page_size: int = 500,
) -> None:
    async def source_metadata_for_series(
        _session: AsyncSession,
        _item: ImportedSeries,
    ) -> SourceMetadata:
        return SourceMetadata(original_title=_item.raw_series_name)

    async def reclassify_duplicates(
        _session: AsyncSession,
        _job: ImportJob,
    ) -> int | None:
        return None

    async def recompute_series_counters(_session: AsyncSession, _job: ImportJob) -> None:
        return None

    async def log_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def emit_progress(
        _session: AsyncSession,
        _job: ImportJob,
        event: ImportProgressEvent,
        callback: Callable[[ImportProgressEvent], Awaitable[None]],
    ) -> None:
        await callback(event)

    async def maybe_slow_item_delay() -> None:
        return None

    await import_series_matching.run_import_series_matching(
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
        item_page_size=item_page_size,
        file_page_size=file_page_size,
        profile_page_size=profile_page_size,
    )


async def test_series_matching_pages_pending_rows_and_progress_profiles(
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
            series_found=11,
        )
        seed_session.add(job)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Series {index:02d}",
                    raw_year=2000 + index,
                    status=ImportSeriesStatus.PENDING,
                    has_files=False,
                )
                for index in range(11)
            ]
        )
        await seed_session.commit()
        job_id = job.id

    item_page_sizes: list[int] = []
    profile_page_sizes: list[int] = []
    original_item_loader = import_series_matching._load_pending_import_series_page
    original_profile_loader = import_series_matching._load_scan_review_profile_page

    async def record_item_page(*args: Any, **kwargs: Any) -> list[ImportedSeries]:
        rows = await original_item_loader(*args, **kwargs)
        item_page_sizes.append(len(rows))
        return rows

    async def record_profile_page(
        *args: Any,
        **kwargs: Any,
    ) -> list[import_series_matching._ScanReviewProfile]:
        rows = await original_profile_loader(*args, **kwargs)
        profile_page_sizes.append(len(rows))
        return rows

    monkeypatch.setattr(
        import_series_matching,
        "_load_pending_import_series_page",
        record_item_page,
    )
    monkeypatch.setattr(
        import_series_matching,
        "_load_scan_review_profile_page",
        record_profile_page,
    )

    evaluated: list[str] = []
    cancellation_checks = 0
    progress_events: list[ImportProgressEvent] = []

    async def evaluate_match(**kwargs: Any) -> ComicVineMatchEvaluation:
        raw_name = str(kwargs["raw_name"])
        evaluated.append(raw_name)
        return ComicVineMatchEvaluation(
            match={
                "cv_id": 1000 + len(evaluated),
                "cv_title": raw_name,
                "cv_year": kwargs.get("raw_year"),
                "cv_publisher": "Test Publisher",
                "cv_issue_count": 0,
                "cv_url": f"https://example.com/{len(evaluated)}",
                "cv_match_score": 1.0,
                "cv_match_method": "exact_title_year",
            },
            diagnostics={},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    async def progress_callback(event: ImportProgressEvent) -> None:
        progress_events.append(event)

    async with factory() as session:
        matching_job = await session.get(ImportJob, job_id)
        assert matching_job is not None
        await _run_matching(
            session,
            matching_job,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            progress_callback=progress_callback,
            item_page_size=3,
            profile_page_size=4,
        )
        await session.commit()

    assert item_page_sizes == [3, 3, 3, 2, 0]
    assert profile_page_sizes == [4, 4, 3, 0]
    assert evaluated == [f"Series {index:02d}" for index in range(11)]
    assert cancellation_checks >= 11 + len(item_page_sizes) + len(profile_page_sizes)
    assert any(event.message == "Matching 11/11..." for event in progress_events)

    async with factory() as verify_session:
        matched = await verify_session.scalar(
            select(func.count(ImportedSeries.id)).where(
                ImportedSeries.import_job_id == job_id,
                ImportedSeries.status == ImportSeriesStatus.MATCHED,
            )
        )
    assert matched == 11


async def test_series_no_match_updates_files_in_bounded_pages(
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as seed_session:
        job = ImportJob(
            source_path="/tmp/imports",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_started_at=datetime.now(UTC),
            series_found=1,
        )
        seed_session.add(job)
        await seed_session.flush()
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Unknown Series",
            raw_year=2025,
            status=ImportSeriesStatus.PENDING,
            file_count=23,
            files_total=23,
        )
        seed_session.add(item)
        await seed_session.flush()
        seed_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path=f"/tmp/imports/Unknown Series {index:03d}.cbz",
                    file_name=f"Unknown Series {index:03d}.cbz",
                    file_format="cbz",
                    status=ImportedFileStatus.PENDING,
                )
                for index in range(23)
            ]
        )
        await seed_session.commit()
        job_id = job.id

    file_page_sizes: list[int] = []
    original_file_loader = import_series_matching._load_pending_import_file_page

    async def record_file_page(*args: Any, **kwargs: Any) -> list[ImportedFile]:
        rows = await original_file_loader(*args, **kwargs)
        file_page_sizes.append(len(rows))
        return rows

    monkeypatch.setattr(
        import_series_matching,
        "_load_pending_import_file_page",
        record_file_page,
    )

    async def evaluate_match(**_kwargs: Any) -> ComicVineMatchEvaluation:
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={"kind": "series_no_match", "reason": "not_found"},
        )

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        return None

    async with factory() as session:
        matching_job = await session.get(ImportJob, job_id)
        assert matching_job is not None
        await _run_matching(
            session,
            matching_job,
            evaluate_match=evaluate_match,
            raise_if_cancelled=raise_if_cancelled,
            item_page_size=1,
            file_page_size=5,
        )
        await session.commit()

    assert file_page_sizes == [5, 5, 5, 5, 3, 0]
    async with factory() as verify_session:
        no_match = await verify_session.scalar(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.NO_MATCH,
                ImportedFile.include_in_import.is_(False),
            )
        )
    assert no_match == 23
