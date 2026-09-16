"""Scale and cancellation contracts for bounded import-series deduplication."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import Series
from pullbox.services import import_series_deduplication as deduplication
from pullbox.services.import_progress_runtime import (
    ScanReviewFileMatchProfile,
    ScanReviewSeriesMatchProfile,
    scan_review_progress_pct,
    scan_review_progress_plan,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent


async def _noop_log_event(*_args: object, **_kwargs: object) -> None:
    return None


async def _emit_progress(
    _session: AsyncSession,
    _job: ImportJob,
    progress: ImportProgressEvent,
    callback: Callable[[ImportProgressEvent], Awaitable[None]],
) -> None:
    await callback(progress)


def _job_stats(_job: ImportJob) -> dict[str, int]:
    return {}


async def _create_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/imports/scale",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.ANALYZING,
    )
    session.add(job)
    await session.flush()
    return job


async def test_deduplication_keyset_pages_generated_scale_without_result_drift(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated jobs must not retain an unbounded ORM or candidate result set."""
    target = Series(
        title="Scale Target",
        sort_title="scale target",
        year_start=2020,
        comicvine_id=991_001,
    )
    db_session.add(target)
    await db_session.flush()
    job = await _create_job(db_session)

    rows = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=("Scale Target" if index == 500 else f"Unrelated {index:04d}"),
            raw_year=2020,
            status=ImportSeriesStatus.PENDING,
            file_count=index % 7,
            files_total=index % 7,
            has_files=True,
        )
        for index in range(1_003)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    rows.clear()

    observed: dict[str, list[int]] = {
        "items": [],
        "profiles": [],
        "candidates": [],
    }

    async def record_page(
        key: str,
        original: Callable[..., Awaitable[list[Any]]],
        *args: object,
        **kwargs: object,
    ) -> list[Any]:
        page = await original(*args, **kwargs)
        observed[key].append(len(page))
        return page

    original_items = deduplication._load_pending_import_series_page
    original_profiles = deduplication._load_scan_review_profile_page
    original_candidates = deduplication._load_existing_name_candidate_page

    async def load_items(*args: object, **kwargs: object) -> list[Any]:
        return await record_page("items", original_items, *args, **kwargs)

    async def load_profiles(*args: object, **kwargs: object) -> list[Any]:
        return await record_page("profiles", original_profiles, *args, **kwargs)

    async def load_candidates(*args: object, **kwargs: object) -> list[Any]:
        return await record_page("candidates", original_candidates, *args, **kwargs)

    monkeypatch.setattr(deduplication, "_load_pending_import_series_page", load_items)
    monkeypatch.setattr(deduplication, "_load_scan_review_profile_page", load_profiles)
    monkeypatch.setattr(
        deduplication,
        "_load_existing_name_candidate_page",
        load_candidates,
    )

    cancellation_checks = 0

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    progress_events: list[ImportProgressEvent] = []

    async def capture_progress(progress: ImportProgressEvent) -> None:
        progress_events.append(progress)

    await deduplication.deduplicate_import_series(
        db_session,
        job,
        raise_if_cancelled=raise_if_cancelled,
        log_event=_noop_log_event,
        emit_progress=_emit_progress,
        phase_progress=lambda *_args: 0,
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=_job_stats,
        progress_callback=capture_progress,
        item_page_size=17,
        candidate_page_size=19,
        profile_page_size=23,
    )

    duplicate_count = await db_session.scalar(
        select(func.count(ImportedSeries.id)).where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
        )
    )
    matched = await db_session.scalar(
        select(ImportedSeries).where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.raw_series_name == "Scale Target",
        )
    )

    assert duplicate_count == 1
    assert matched is not None
    assert matched.series_id == target.id
    assert matched.cv_id == target.comicvine_id
    assert max(observed["items"]) <= 17
    assert max(observed["profiles"]) <= 23
    assert max(observed["candidates"]) <= 19
    assert len(observed["items"]) > 50
    assert len(observed["profiles"]) > 40
    assert len(observed["candidates"]) > 50
    assert cancellation_checks > len(observed["items"])
    assert progress_events
    assert progress_events[-1].message == "Analyzing 1003/1003..."
    legacy_plan = scan_review_progress_plan(
        analysis_series_count=1_003,
        series_match_profiles=[
            ScanReviewSeriesMatchProfile(file_count=index % 7, direct_match=False)
            for index in range(1_003)
        ],
        file_match_profiles=[
            ScanReviewFileMatchProfile(file_count=index % 7, issue_count=None)
            for index in range(1_003)
        ],
    )
    assert progress_events[-1].progress == scan_review_progress_pct(
        legacy_plan,
        completed_weight=legacy_plan.analysis_weight,
    )


async def test_deduplication_keeps_comicvine_identity_precedence(
    db_session: AsyncSession,
) -> None:
    """Exact trusted identity must win even when a name/year candidate sorts first."""
    by_name = Series(
        title="Identity Target",
        sort_title="identity target",
        year_start=2020,
        comicvine_id=991_010,
    )
    by_cv_id = Series(
        title="Different Canonical Title",
        sort_title="different canonical title",
        year_start=1995,
        comicvine_id=991_011,
    )
    db_session.add_all([by_name, by_cv_id])
    await db_session.flush()
    job = await _create_job(db_session)
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Identity Target",
        raw_year=2020,
        cv_id=by_cv_id.comicvine_id,
        status=ImportSeriesStatus.PENDING,
        file_count=1,
    )
    db_session.add(item)
    await db_session.commit()

    await deduplication.deduplicate_import_series(
        db_session,
        job,
        raise_if_cancelled=_noop_log_event,
        log_event=_noop_log_event,
        emit_progress=_emit_progress,
        phase_progress=lambda *_args: 0,
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=_job_stats,
    )

    await db_session.refresh(item)
    assert item.series_id == by_cv_id.id
    assert item.cv_id == by_cv_id.comicvine_id
    assert item.diagnostics["duplicate_reason"] == "cv_id"


async def test_deduplication_cancellation_stops_after_a_committed_page(
    db_session: AsyncSession,
) -> None:
    """A cancellation checkpoint must bound loss to the current page."""
    target = Series(
        title="Canonical",
        sort_title="canonical",
        year_start=2020,
        comicvine_id=991_002,
    )
    db_session.add(target)
    await db_session.flush()
    job = await _create_job(db_session)
    db_session.add_all(
        [
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Incoming {index:02d}",
                raw_year=2020,
                cv_id=target.comicvine_id,
                status=ImportSeriesStatus.PENDING,
                file_count=1,
            )
            for index in range(20)
        ]
    )
    await db_session.commit()

    commit_count = 0

    def after_commit(_session: object) -> None:
        nonlocal commit_count
        commit_count += 1

    event.listen(db_session.sync_session, "after_commit", after_commit)
    cancellation_checks = 0

    async def cancel_before_second_page(_session: AsyncSession, _job_id: int) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise RuntimeError("cancelled at page boundary")

    try:
        with pytest.raises(RuntimeError, match="page boundary"):
            await deduplication.deduplicate_import_series(
                db_session,
                job,
                raise_if_cancelled=cancel_before_second_page,
                log_event=_noop_log_event,
                emit_progress=_emit_progress,
                phase_progress=lambda *_args: 0,
                estimate_remaining_seconds=lambda *_args: None,
                job_stats=_job_stats,
                item_page_size=7,
                candidate_page_size=5,
                profile_page_size=5,
            )
    finally:
        event.remove(db_session.sync_session, "after_commit", after_commit)

    status_rows = await db_session.execute(
        select(ImportedSeries.status, func.count(ImportedSeries.id))
        .where(ImportedSeries.import_job_id == job.id)
        .group_by(ImportedSeries.status)
    )
    status_counts: dict[ImportSeriesStatus, int] = {
        status: int(count) for status, count in status_rows.tuples()
    }
    assert commit_count == 1
    assert status_counts == {
        ImportSeriesStatus.DUPLICATE: 7,
        ImportSeriesStatus.PENDING: 13,
    }
    await db_session.refresh(job)
    assert job.series_duplicate == 7

    await deduplication.deduplicate_import_series(
        db_session,
        job,
        raise_if_cancelled=_noop_log_event,
        log_event=_noop_log_event,
        emit_progress=_emit_progress,
        phase_progress=lambda *_args: 0,
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=_job_stats,
        item_page_size=7,
        candidate_page_size=5,
        profile_page_size=5,
    )
    await db_session.refresh(job)
    remaining = await db_session.scalar(
        select(func.count(ImportedSeries.id)).where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status == ImportSeriesStatus.PENDING,
        )
    )
    assert remaining == 0
    assert job.series_duplicate == 20


async def test_exact_title_issue_target_fallback_uses_bounded_pages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = Series(
        title="Long Running",
        sort_title="long running",
        year_start=2020,
        comicvine_id=991_020,
    )
    db_session.add(existing)
    await db_session.flush()
    db_session.add_all(
        [
            Issue(
                series_id=existing.id,
                issue_number=float(index),
                issue_number_text=str(index),
                release_date=date(2026, 1, 1),
                status=IssueStatus.SKIPPED,
                issue_type=IssueType.ISSUE,
            )
            for index in range(1, 54)
        ]
    )
    job = await _create_job(db_session)
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Long Running",
        raw_year=2026,
        status=ImportSeriesStatus.PENDING,
        file_count=53,
        files_total=53,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path=f"/imports/Long Running {index}.cbz",
                file_name=f"Long Running {index}.cbz",
                file_format="cbz",
                parsed_issue_number=float(index),
                issue_number_raw=str(index),
                parsed_year=2026,
                status=ImportedFileStatus.PENDING,
            )
            for index in range(1, 54)
        ]
    )
    await db_session.commit()

    observed_page_sizes: list[int] = []
    original_loader = deduplication._load_imported_issue_target_page

    async def record_page(
        session: AsyncSession,
        *,
        import_series_id: int,
        after_id: int,
        page_size: int,
    ) -> list[deduplication._ImportedIssueTargetRow]:
        page = await original_loader(
            session,
            import_series_id=import_series_id,
            after_id=after_id,
            page_size=page_size,
        )
        observed_page_sizes.append(len(page))
        return page

    monkeypatch.setattr(
        deduplication,
        "_load_imported_issue_target_page",
        record_page,
    )
    cancellation_checks = 0

    async def raise_if_cancelled(_session: AsyncSession, _job_id: int) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    await deduplication.deduplicate_import_series(
        db_session,
        job,
        raise_if_cancelled=raise_if_cancelled,
        log_event=_noop_log_event,
        emit_progress=_emit_progress,
        phase_progress=lambda *_args: 0,
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=_job_stats,
        issue_target_page_size=7,
    )

    await db_session.refresh(item)
    assert item.status == ImportSeriesStatus.DUPLICATE
    assert item.diagnostics["duplicate_reason"] == "exact_title_issue_target"
    assert observed_page_sizes == [7, 7, 7, 7, 7, 7, 7, 4, 0]
    assert cancellation_checks >= len(observed_page_sizes)


async def test_exact_title_issue_target_fallback_preserves_suffix_identity(
    db_session: AsyncSession,
) -> None:
    existing = Series(
        title="Suffix Series",
        sort_title="suffix series",
        year_start=2020,
        comicvine_id=991_021,
    )
    db_session.add(existing)
    await db_session.flush()
    db_session.add_all(
        [
            Issue(
                series_id=existing.id,
                issue_number=1.0,
                issue_number_text=exact_text,
                release_date=date(2026, 1, 1),
                status=IssueStatus.SKIPPED,
                issue_type=IssueType.ISSUE,
            )
            for exact_text in ("1AU", "1B")
        ]
    )
    job = await _create_job(db_session)
    items: list[ImportedSeries] = []
    for index, exact_text in enumerate(("1B", "1C", None)):
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Suffix Series",
            raw_year=2026,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            files_total=1,
        )
        db_session.add(item)
        await db_session.flush()
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=item.id,
                file_path=f"/imports/Suffix Series {index}.cbz",
                file_name=f"Suffix Series {index}.cbz",
                file_format="cbz",
                parsed_issue_number=1.0,
                issue_number_raw=exact_text,
                parsed_year=2026,
                status=ImportedFileStatus.PENDING,
            )
        )
        items.append(item)
    await db_session.commit()

    await deduplication.deduplicate_import_series(
        db_session,
        job,
        raise_if_cancelled=_noop_log_event,
        log_event=_noop_log_event,
        emit_progress=_emit_progress,
        phase_progress=lambda *_args: 0,
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=_job_stats,
        issue_target_page_size=2,
    )

    for item in items:
        await db_session.refresh(item)
    assert [item.status for item in items] == [
        ImportSeriesStatus.DUPLICATE,
        ImportSeriesStatus.PENDING,
        ImportSeriesStatus.PENDING,
    ]
