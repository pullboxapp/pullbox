"""Scale and recovery guards for bounded import file matching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.exceptions import JobCancelledError
from pullbox.models.base import Base
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
from pullbox.models.series import Series
from pullbox.services import import_file_matching as file_matching
from pullbox.services.import_progress_runtime import scan_review_progress_pct
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


class _ProviderSpy:
    def __init__(self) -> None:
        self.metadata_calls: list[str] = []

    def cache_metrics(self) -> dict[str, int]:
        return {}

    async def get_series(self, provider_id: str) -> None:
        self.metadata_calls.append(f"get_series:{provider_id}")
        raise AssertionError("local file matching must not call the metadata provider")

    async def get_issue(self, provider_id: str) -> None:
        self.metadata_calls.append(f"get_issue:{provider_id}")
        raise AssertionError("local file matching must not call the metadata provider")

    async def get_issues_for_series(self, provider_id: str) -> None:
        self.metadata_calls.append(f"get_issues_for_series:{provider_id}")
        raise AssertionError("local file matching must not call the metadata provider")


def _service_with_provider_spy() -> tuple[ImportService, _ProviderSpy]:
    provider = _ProviderSpy()
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=SimpleNamespace(_provider=provider),  # type: ignore[arg-type]
        event_bus=AsyncMock(),
    )
    return service, provider


async def _canonical_series(
    session: AsyncSession,
    *,
    title: str,
    issue_count: int,
) -> tuple[Series, list[Issue]]:
    series = Series(
        title=title,
        sort_title=title.casefold(),
        year_start=2020,
        comicvine_id=800_000 + issue_count,
        issue_count=issue_count,
    )
    session.add(series)
    await session.flush()
    issues = [
        Issue(
            series_id=series.id,
            issue_number=float(index),
            comicvine_id=900_000 + index,
            title=f"Issue {index}",
            status=IssueStatus.WANTED,
        )
        for index in range(1, issue_count + 1)
    ]
    session.add_all(issues)
    await session.flush()
    return series, issues


async def _job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/generated",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FILE_MATCHING,
    )
    session.add(job)
    await session.flush()
    return job


@pytest.mark.parametrize("source_type", [ImportSourceType.MYLAR3, ImportSourceType.FILESYSTEM])
async def test_blocked_only_review_is_batched_and_progress_counts_remaining_summary_work(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, source_type: ImportSourceType
) -> None:
    job = await _job(db_session)
    job.source_type = source_type
    job.progress_snapshot = {"progress": 49, "phase": "matching"}
    job.total_files_found = 48
    job.scan_started_at = datetime.now(UTC) - timedelta(minutes=10)
    job.scan_completed_at = datetime.now(UTC) - timedelta(minutes=5)
    groups = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Blocked {i}",
            status=ImportSeriesStatus.MATCHED,
            cv_id=1000 + i,
            file_count=4,
        )
        for i in range(12)
    ]
    db_session.add_all(groups)
    await db_session.flush()
    files = []
    for group in groups:
        for i in range(4):
            item = _imported_file(
                job_id=job.id,
                import_series_id=group.id,
                ordinal=i,
                issue_number=float(i + 1),
                series_name=group.raw_series_name,
            )
            item.status = ImportedFileStatus.SAFETY_BLOCKED
            item.diagnostics = {"safety_block": {"kind": "single_page_comic"}}
            files.append(item)
    db_session.add_all(files)
    await db_session.commit()
    service, provider = _service_with_provider_spy()
    plan = await file_matching._build_file_matching_plan(
        db_session, job, series_ids=None, raise_if_cancelled=AsyncMock(), page_size=5
    )
    assert (
        scan_review_progress_pct(
            plan.progress_plan, completed_weight=plan.completed_file_match_weight
        )
        == 49
    )
    assert plan.progress_plan.total_weight > plan.completed_file_match_weight
    original_summary = file_matching._summarize_import_series_files
    summary_spy = AsyncMock(wraps=original_summary)
    count_spy = AsyncMock(wraps=file_matching._count_import_series_files)
    monkeypatch.setattr(file_matching, "_summarize_import_series_files", summary_spy)
    monkeypatch.setattr(file_matching, "_count_import_series_files", count_spy)
    monkeypatch.setattr(file_matching, "_SERIES_PAGE_SIZE", 5)
    events = []
    eta_starts = []
    started = datetime.now(UTC)

    def estimate(started_at, *, completed_units, total_units, **_kwargs):
        eta_starts.append(started_at)
        return 10 if 0 < completed_units < total_units else None

    monkeypatch.setattr(file_matching, "estimate_remaining_work_seconds", estimate)
    finalize = file_matching._rebuild_cross_series_conflicts

    async def check_finalization(*args, **kwargs):
        assert events[-1].progress < 99
        assert "Finalizing" in events[-1].message
        return await finalize(*args, **kwargs)

    monkeypatch.setattr(file_matching, "_rebuild_cross_series_conflicts", check_finalization)

    async def capture(event):
        events.append(event)
        assert job.total_files_found == 48

    await service._run_file_matching(db_session, job, progress_callback=capture)
    assert summary_spy.await_count == 0
    assert count_spy.await_count == 0
    assert not provider.metadata_calls
    assert [e.progress for e in events] == sorted(e.progress for e in events)
    assert events[0].progress == 49
    assert events[-1].progress == 99
    assert any(49 < e.progress < 99 for e in events)
    assert any(e.estimated_seconds_remaining == 10 for e in events)
    assert all(value >= started for value in eta_starts)
    assert any("review" in e.message.lower() for e in events)
    assert all(group.status == ImportSeriesStatus.MATCHED for group in groups)
    assert all(group.files_total == 4 and group.files_matched == 0 for group in groups)
    assert all(item.status == ImportedFileStatus.SAFETY_BLOCKED for item in files)
    assert all(
        item.diagnostics == {"safety_block": {"kind": "single_page_comic"}} for item in files
    )


async def test_blocked_summary_batch_excludes_mixed_linked_and_identity_conflict_groups(db_session):
    job = await _job(db_session)
    canonical, _issues = await _canonical_series(db_session, title="Existing", issue_count=1)
    groups = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Case {i}",
            status=ImportSeriesStatus.MATCHED,
            cv_id=500 + i,
        )
        for i in range(6)
    ]
    groups[4].series_id = canonical.id
    groups[5].status = ImportSeriesStatus.DUPLICATE
    db_session.add_all(groups)
    await db_session.flush()
    for i, group in enumerate(groups):
        item = _imported_file(
            job_id=job.id,
            import_series_id=group.id,
            ordinal=i,
            issue_number=1,
            series_name=group.raw_series_name,
        )
        item.status = ImportedFileStatus.SAFETY_BLOCKED
        item.diagnostics = {"safety_block": {"kind": "single_page_comic"}}
        if i in (2, 3):
            item.diagnostics = {"kind": "metadata_conflict" if i == 2 else "source_layout_review"}
        db_session.add(item)
    db_session.add(
        _imported_file(
            job_id=job.id,
            import_series_id=groups[1].id,
            ordinal=10,
            issue_number=2,
            series_name="Mixed",
        )
    )
    await db_session.flush()
    assert await file_matching._blocked_only_summary_counts(db_session, groups) == {groups[0].id: 1}
    summary = await file_matching._summarize_import_series_files(
        db_session,
        groups[0],
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=job.cv_match_threshold,
        raise_if_cancelled=AsyncMock(),
        job_id=job.id,
    )
    assert summary == file_matching.FileMatchSeriesSummary(1, 0, 0, 0, 0, 0)
    assert groups[0].status == ImportSeriesStatus.MATCHED


async def test_blocked_summary_pages_check_cancellation_before_loading_next_page(db_session):
    job = await _job(db_session)
    db_session.add_all(
        [
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Case {i}",
                status=ImportSeriesStatus.MATCHED,
                cv_id=1000 + i,
            )
            for i in range(11)
        ]
    )
    await db_session.flush()
    checks = AsyncMock(side_effect=[None, JobCancelledError("cancelled")])
    seen = []
    with pytest.raises(JobCancelledError):
        async for _, item, _ in file_matching._iter_eligible_import_series(
            db_session,
            job_id=job.id,
            series_ids=None,
            raise_if_cancelled=checks,
            page_size=5,
            blocked_only_counts={},
        ):
            seen.append(item.id)
    assert len(seen) == 5


async def test_split_matching_invalidates_cached_blocked_summary_on_same_page(
    db_session, monkeypatch
):
    job = await _job(db_session)
    canonical, _ = await _canonical_series(db_session, title="Parent", issue_count=2)
    parent = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Parent",
        series_id=canonical.id,
        status=ImportSeriesStatus.MATCHED,
        file_count=2,
    )
    children = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Child {i}",
            status=ImportSeriesStatus.MATCHED,
            cv_id=4000 + i,
            file_count=1,
            cv_match_method="mylar3_cv_id",
        )
        for i in range(2)
    ]
    db_session.add_all([parent, *children])
    await db_session.flush()
    for i, group in enumerate([parent, *children]):
        for n in range(2 if i == 0 else 1):
            item = _imported_file(
                job_id=job.id,
                import_series_id=group.id,
                ordinal=n,
                issue_number=float(n + 1),
                series_name=group.raw_series_name,
            )
            if i:
                item.status = ImportedFileStatus.SAFETY_BLOCKED
            db_session.add(item)
    await db_session.commit()

    async def move_to_child_reviews(_session, _job, group, files, **_kwargs):
        assert group.id == parent.id
        for item, child in zip(files, children, strict=True):
            item.import_series_id = child.id
            item.status = ImportedFileStatus.NO_MATCH
            child.file_count += 1
        parent.file_count = 0
        parent.status = ImportSeriesStatus.SKIPPED
        return [], [child.id for child in children]

    monkeypatch.setattr(
        file_matching, "_split_explicit_issue_series_mismatches", move_to_child_reviews
    )
    monkeypatch.setattr(file_matching, "_SERIES_PAGE_SIZE", 2)
    service, provider = _service_with_provider_spy()

    async def check_checkpoint(event):
        if event.message == "Prepared file review for Child 0":
            assert children[0].files_total == 2
            assert children[0].files_no_match == 1

    await service._run_file_matching(db_session, job, progress_callback=check_checkpoint)
    assert not provider.metadata_calls
    for child in children:
        await db_session.refresh(child)
        assert child.files_total == 2
        assert child.files_no_match == 1


def _imported_file(
    *,
    job_id: int,
    import_series_id: int,
    ordinal: int,
    issue_number: float,
    series_name: str,
    file_size: int | None = None,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job_id,
        import_series_id=import_series_id,
        file_path=f"/generated/{series_name}-{ordinal:05d}.cbz",
        file_name=f"{series_name} {ordinal:05d} Issue {issue_number:g}.cbz",
        file_size=file_size if file_size is not None else 1_000 + ordinal,
        file_format="cbz",
        parsed_series=series_name,
        parsed_issue_number=issue_number,
        status=ImportedFileStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_generated_scale_uses_keyset_pages_and_bounded_target_indexes(
    db_session: AsyncSession,
) -> None:
    generated_count = 1_003
    canonical, _issues = await _canonical_series(
        db_session,
        title="Generated Scale",
        issue_count=generated_count,
    )
    job = await _job(db_session)
    imported_series = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Generated Scale {index:05d}",
            status=ImportSeriesStatus.MATCHED,
            series_id=canonical.id,
            file_count=generated_count if index == 0 else 0,
            files_total=generated_count if index == 0 else 0,
        )
        for index in range(generated_count)
    ]
    db_session.add_all(imported_series)
    await db_session.flush()
    primary = imported_series[0]
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=primary.id,
            ordinal=index,
            issue_number=float(index),
            series_name="Generated Scale",
        )
        for index in range(1, generated_count + 1)
    ]
    db_session.add_all(files)

    pathological = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Pathological Cohort",
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=file_matching._MAX_IDENTITY_COHORT_SIZE + 1,
    )
    db_session.add(pathological)
    await db_session.flush()
    db_session.add_all(
        [
            _imported_file(
                job_id=job.id,
                import_series_id=pathological.id,
                ordinal=index,
                issue_number=1.0,
                series_name="Pathological Cohort",
            )
            for index in range(file_matching._MAX_IDENTITY_COHORT_SIZE + 1)
        ]
    )
    await db_session.flush()

    seen_series_ids: list[int] = []
    after_id = 0
    max_series_page = 0
    series_page_count = 0
    while True:
        series_page = await file_matching._load_eligible_import_series_page(
            db_session,
            job_id=job.id,
            after_id=after_id,
            page_size=37,
            series_ids=None,
        )
        if not series_page:
            break
        max_series_page = max(max_series_page, len(series_page))
        series_page_count += 1
        seen_series_ids.extend(item.id for item in series_page)
        after_id = series_page[-1].id

    profile_ids: list[int] = []
    after_id = 0
    max_profile_page = 0
    profile_page_count = 0
    while True:
        profiles = await file_matching._load_file_matching_profile_page(
            db_session,
            job_id=job.id,
            after_id=after_id,
            page_size=43,
            series_ids=None,
        )
        if not profiles:
            break
        max_profile_page = max(max_profile_page, len(profiles))
        profile_page_count += 1
        profile_ids.extend(profile.id for profile in profiles)
        after_id = profiles[-1].id

    file_ids: list[int] = []
    file_pages: list[list[ImportedFile]] = []
    after_id = 0
    while True:
        file_page = await file_matching._load_eligible_import_file_page(
            db_session,
            import_series_id=primary.id,
            after_id=after_id,
            page_size=41,
        )
        if not file_page:
            break
        file_pages.append(file_page)
        file_ids.extend(item.id for item in file_page)
        after_id = file_page[-1].id

    target_index = await file_matching._load_existing_series_page_target_index(
        db_session,
        primary,
        file_pages[0],
    )
    distinct_cohort_values: list[float] = []
    distinct_cohort_pages = 0
    after_value: str | None = None
    while True:
        cohort_page = await file_matching._load_file_target_cohort_page(
            db_session,
            job_id=job.id,
            import_series_id=primary.id,
            kind="parsed_issue",
            after_value=after_value,
            page_size=31,
        )
        if not cohort_page:
            break
        distinct_cohort_pages += 1
        distinct_cohort_values.extend(float(cohort.value) for cohort in cohort_page)
        after_value = cohort_page[-1].value

    first_cohort = await file_matching._load_file_target_cohort_page(
        db_session,
        job_id=job.id,
        import_series_id=primary.id,
        kind="parsed_issue",
        after_value=None,
        page_size=1,
    )
    first_cohort_files = await file_matching._load_file_target_cohort(
        db_session,
        job_id=job.id,
        import_series_id=primary.id,
        cohort=first_cohort[0],
    )
    cohorts = await file_matching._load_file_target_cohort_page(
        db_session,
        job_id=job.id,
        import_series_id=pathological.id,
        kind="parsed_issue",
        after_value=None,
        page_size=2,
    )

    grouped_scan_count = 0

    def count_grouped_scans(
        _conn: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        context: object,
        _executemany: bool,
    ) -> None:
        nonlocal grouped_scan_count
        execution_options = getattr(context, "execution_options", {})
        if execution_options.get("pullbox_cohort_scan") == "file_target":
            grouped_scan_count += 1

    assert db_session.bind is not None
    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", count_grouped_scans)
    streamed_cohorts: list[file_matching._FileTargetCohort] = []

    async def never_cancel(_session: AsyncSession, _job_id: int) -> None:
        return None

    try:
        async for batch in file_matching._iter_file_target_cohort_batches(
            db_session,
            job_id=job.id,
            import_series_id=primary.id,
            kind="parsed_issue",
            batch_size=31,
            raise_if_cancelled=never_cancel,
        ):
            assert len(batch) <= 31
            streamed_cohorts.extend(batch)
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_grouped_scans)

    assert max_series_page == 37
    assert series_page_count == (generated_count + 1 + 36) // 37
    assert seen_series_ids == sorted(seen_series_ids)
    assert len(seen_series_ids) == generated_count + 1
    assert max_profile_page == 43
    assert profile_page_count == (generated_count + 1 + 42) // 43
    assert profile_ids == seen_series_ids
    assert max(len(page) for page in file_pages) == 41
    assert len(file_pages) == (generated_count + 40) // 41
    assert file_ids == [item.id for item in files]
    assert len(target_index.number_map) == 41
    assert len(target_index.issue_entries) == 0
    assert distinct_cohort_pages == (generated_count + 30) // 31
    assert distinct_cohort_values == [float(index) for index in range(1, generated_count + 1)]
    assert first_cohort_files == [files[0]]
    assert len(cohorts) == 1
    assert cohorts[0].file_count == file_matching._MAX_IDENTITY_COHORT_SIZE + 1
    assert grouped_scan_count == 1
    assert [float(cohort.value) for cohort in streamed_cohorts] == [
        float(index) for index in range(1, generated_count + 1)
    ]
    with pytest.raises(file_matching._ImportFileMatchingCohortLimitError):
        await file_matching._load_file_target_cohort(
            db_session,
            job_id=job.id,
            import_series_id=pathological.id,
            cohort=cohorts[0],
        )


@pytest.mark.asyncio
async def test_local_target_index_does_not_guess_between_exact_text_collisions(
    db_session: AsyncSession,
) -> None:
    canonical, _issues = await _canonical_series(
        db_session,
        title="Exact Number Collision",
        issue_count=0,
    )
    colliding_issues = [
        Issue(
            series_id=canonical.id,
            issue_number=1.0,
            issue_number_text=exact_text,
            comicvine_id=910_000 + index,
            title=f"Issue {exact_text}",
            status=IssueStatus.WANTED,
        )
        for index, exact_text in enumerate(("1A", "1B"), start=1)
    ]
    db_session.add_all(colliding_issues)
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=1,
        files_total=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imp_file = _imported_file(
        job_id=job.id,
        import_series_id=imported_series.id,
        ordinal=1,
        issue_number=1.0,
        series_name=canonical.title,
    )
    imp_file.issue_number_raw = "1A"
    db_session.add(imp_file)
    await db_session.flush()

    target_index = await file_matching._load_existing_series_page_target_index(
        db_session,
        imported_series,
        [imp_file],
    )

    assert 1.0 not in target_index.number_map
    assert set(target_index.exact_number_map) == {"1A", "1B"}
    assert set(target_index.cv_id_map) == {issue.comicvine_id for issue in colliding_issues}
    exact_candidate = file_matching.select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert exact_candidate is not None
    assert exact_candidate.matched_issue_id == colliding_issues[0].id

    imp_file.issue_number_raw = None
    assert (
        file_matching.select_file_match_candidate(
            imp_file,
            target_index,
            series_high_confidence=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_local_exact_issue_text_does_not_fall_back_to_numeric_issue(
    db_session: AsyncSession,
) -> None:
    canonical, issues = await _canonical_series(
        db_session,
        title="Exact Number Miss",
        issue_count=1,
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=1,
        files_total=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imp_file = _imported_file(
        job_id=job.id,
        import_series_id=imported_series.id,
        ordinal=1,
        issue_number=1.0,
        series_name=canonical.title,
    )
    imp_file.issue_number_raw = "1AU"
    db_session.add(imp_file)
    await db_session.flush()

    target_index = await file_matching._load_existing_series_page_target_index(
        db_session,
        imported_series,
        [imp_file],
    )

    assert target_index.number_map[1.0][0] == issues[0].id
    assert (
        file_matching.select_file_match_candidate(
            imp_file,
            target_index,
            series_high_confidence=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_local_target_index_fails_closed_before_raw_rows_exceed_bound(
    db_session: AsyncSession,
) -> None:
    canonical, _issues = await _canonical_series(
        db_session,
        title="Bounded Target Collision",
        issue_count=0,
    )

    def suffix_for(index: int) -> str:
        value = index
        letters = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    db_session.add_all(
        [
            Issue(
                series_id=canonical.id,
                issue_number=1.0,
                issue_number_text=f"1{suffix_for(index)}",
                comicvine_id=920_000 + index,
                title=f"Collision {index}",
                status=IssueStatus.WANTED,
            )
            for index in range(1, file_matching._MAX_TARGET_INDEX_ENTRIES + 2)
        ]
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=1,
        files_total=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imp_file = _imported_file(
        job_id=job.id,
        import_series_id=imported_series.id,
        ordinal=1,
        issue_number=1.0,
        series_name=canonical.title,
    )
    db_session.add(imp_file)
    await db_session.flush()

    with pytest.raises(file_matching._ImportFileMatchingCohortLimitError):
        await file_matching._load_existing_series_page_target_index(
            db_session,
            imported_series,
            [imp_file],
        )


@pytest.mark.asyncio
async def test_zero_issue_placeholder_uses_series_count_not_page_size(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyIssueProvider:
        async def get_issues_for_series(self, _series_provider_id: str) -> list[object]:
            return []

    provider = EmptyIssueProvider()
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=SimpleNamespace(_provider=provider),  # type: ignore[arg-type]
        event_bus=AsyncMock(),
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Paged Zero-Issue Special",
        status=ImportSeriesStatus.MATCHED,
        cv_id=930_001,
        cv_title="Paged Zero-Issue Special",
        cv_issue_count=0,
        cv_match_score=1.0,
        cv_match_method="exact_title_year",
        file_count=2,
        files_total=2,
        diagnostics={"source_issue_type": "special"},
    )
    db_session.add(imported_series)
    await db_session.flush()
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=imported_series.id,
            ordinal=index,
            issue_number=1.0,
            series_name=imported_series.raw_series_name,
        )
        for index in range(1, 3)
    ]
    for imp_file in files:
        imp_file.diagnostics = {"source_issue_type": "special"}
    db_session.add_all(files)
    await db_session.flush()
    monkeypatch.setattr(file_matching, "_FILE_PAGE_SIZE", 1)

    await service._run_file_matching(db_session, job)

    assert {imp_file.status for imp_file in files} == {ImportedFileStatus.NO_MATCH}
    assert all(imp_file.match_method != "provider_zero_issue_single_issue" for imp_file in files)


@pytest.mark.asyncio
async def test_zero_issue_placeholder_preserves_pre_split_series_count_on_resume(
    db_session: AsyncSession,
) -> None:
    class EmptyIssueProvider:
        async def get_issues_for_series(self, _series_provider_id: str) -> list[object]:
            return []

    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=SimpleNamespace(_provider=EmptyIssueProvider()),  # type: ignore[arg-type]
        event_bus=AsyncMock(),
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Resumed Zero-Issue Special",
        status=ImportSeriesStatus.MATCHED,
        cv_id=930_002,
        cv_title="Resumed Zero-Issue Special",
        cv_issue_count=0,
        cv_match_score=1.0,
        cv_match_method="exact_title_year",
        file_count=1,
        files_total=2,
        diagnostics={"source_issue_type": "special"},
    )
    db_session.add(imported_series)
    await db_session.flush()
    imp_file = _imported_file(
        job_id=job.id,
        import_series_id=imported_series.id,
        ordinal=1,
        issue_number=1.0,
        series_name=imported_series.raw_series_name,
    )
    imp_file.diagnostics = {"source_issue_type": "special"}
    db_session.add(imp_file)
    await db_session.flush()

    await service._run_file_matching(db_session, job)

    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert imp_file.match_method != "provider_zero_issue_single_issue"


def test_identity_cursor_predicates_compile_inside_grouping_queries() -> None:
    exact_file_cursor = 2_000_000_001
    exact_target_cursor = 2_000_000_003
    exact_issue_cursor = 2_000_000_005
    file_query = file_matching._file_target_cohort_query(
        job_id=1,
        import_series_id=2,
        kind="issue_cv_id",
        after_value=str(exact_file_cursor),
        page_size=31,
    )
    parsed_file_query = file_matching._file_target_cohort_query(
        job_id=1,
        import_series_id=2,
        kind="parsed_issue",
        after_value="500.0",
        page_size=31,
    )
    cross_query = file_matching._cross_conflict_cohort_query(
        job_id=1,
        target_kind="series",
        issue_kind="issue_cv_id",
        after_target_value=exact_target_cursor,
        after_issue_value=str(exact_issue_cursor),
        page_size=31,
    )
    for dialect in (
        sqlite.dialect(),
        postgresql.dialect(),  # type: ignore[no-untyped-call]
    ):
        file_sql = str(file_query.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        parsed_file_sql = str(
            parsed_file_query.compile(
                dialect=dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        cross_sql = str(
            cross_query.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        )
        assert "UNION" not in file_sql
        assert "UNION" not in parsed_file_sql
        assert "UNION" not in cross_sql
        assert f"import_files.matched_issue_cv_id > {exact_file_cursor}" in file_sql
        assert "import_files.parsed_issue_number > 500.0" in parsed_file_sql
        assert f"import_series.series_id > {exact_target_cursor}" in cross_sql
        assert f"import_files.matched_issue_cv_id > {exact_issue_cursor}" in cross_sql


@pytest.mark.asyncio
async def test_file_backed_wal_cohort_scan_closes_reader_before_writer_commits(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cohort-stream.db'}",
        echo=False,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            job = await _job(session)
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="WAL Stream",
                status=ImportSeriesStatus.MATCHED,
                cv_id=940_001,
                file_count=9,
                files_total=9,
            )
            session.add(imported_series)
            await session.flush()
            files = [
                _imported_file(
                    job_id=job.id,
                    import_series_id=imported_series.id,
                    ordinal=index,
                    issue_number=float(index),
                    series_name=imported_series.raw_series_name,
                )
                for index in range(1, 10)
            ]
            for imp_file in files:
                imp_file.status = ImportedFileStatus.MATCHED
            session.add_all(files)
            await session.commit()

            scan_count = 0

            def count_scans(
                _conn: object,
                _cursor: object,
                _statement: str,
                _parameters: object,
                context: object,
                _executemany: bool,
            ) -> None:
                nonlocal scan_count
                execution_options = getattr(context, "execution_options", {})
                if execution_options.get("pullbox_cohort_scan") == "file_target":
                    scan_count += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count_scans)
            batches: list[list[file_matching._FileTargetCohort]] = []

            async def check_control_state(
                writer_session: AsyncSession,
                job_id: int,
            ) -> None:
                assert (
                    await writer_session.scalar(select(ImportJob.id).where(ImportJob.id == job_id))
                    == job_id
                )

            try:
                async for batch in file_matching._iter_file_target_cohort_batches(
                    session,
                    job_id=job.id,
                    import_series_id=imported_series.id,
                    kind="parsed_issue",
                    batch_size=2,
                    raise_if_cancelled=check_control_state,
                ):
                    batches.append(batch)
                    files[0].include_in_import = not files[0].include_in_import
                    await session.commit()
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count_scans)

            assert scan_count == 1
            assert max(len(batch) for batch in batches) == 2
            assert [float(cohort.value) for batch in batches for cohort in batch] == [
                float(index) for index in range(1, 10)
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bounded_summary_keeps_full_source_layout_counts(
    db_session: AsyncSession,
) -> None:
    canonical, _issues = await _canonical_series(
        db_session,
        title="Layout Summary",
        issue_count=1,
    )
    job = await _job(db_session)
    file_count = file_matching._SUMMARY_SAMPLE_LIMIT + 3
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        cv_id=canonical.comicvine_id,
        file_count=file_count,
        files_total=file_count,
    )
    db_session.add(imported_series)
    await db_session.flush()
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=imported_series.id,
            ordinal=index,
            issue_number=1.0,
            series_name=canonical.title,
        )
        for index in range(1, file_count + 1)
    ]
    for imp_file in files:
        imp_file.status = ImportedFileStatus.NO_MATCH
        imp_file.diagnostics = {"kind": "source_layout_review"}
    db_session.add_all(files)
    await db_session.flush()

    async def never_cancel(_session: AsyncSession, _job_id: int) -> None:
        return None

    summary = await file_matching._summarize_import_series_files(
        db_session,
        imported_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=job.cv_match_threshold,
        raise_if_cancelled=never_cancel,
        job_id=job.id,
    )
    diagnostics = summary.invalidation_diagnostics or {}
    assert summary.series_invalidated is True
    assert diagnostics["source_layout_review_files"] == file_count
    assert len(diagnostics["unmatched_files"]) == file_matching._SUMMARY_SAMPLE_LIMIT
    assert diagnostics["unmatched_files_truncated"] is True


@pytest.mark.asyncio
async def test_bounded_summary_observes_late_preserve_series_match_evidence(
    db_session: AsyncSession,
) -> None:
    canonical, _issues = await _canonical_series(
        db_session,
        title="Preserve Summary",
        issue_count=1,
    )
    job = await _job(db_session)
    file_count = file_matching._SUMMARY_SAMPLE_LIMIT + 1
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        cv_id=canonical.comicvine_id,
        cv_title=canonical.title,
        cv_match_method="fuzzy_title",
        file_count=file_count,
        files_total=file_count,
    )
    db_session.add(imported_series)
    await db_session.flush()
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=imported_series.id,
            ordinal=index,
            issue_number=1.0,
            series_name=canonical.title,
        )
        for index in range(1, file_count + 1)
    ]
    for imp_file in files:
        imp_file.status = ImportedFileStatus.NO_MATCH
        imp_file.diagnostics = {
            "kind": "metadata_conflict",
            "conflict_type": "series_title",
            "rejection_reason": "generated mismatch",
        }
    files[-1].diagnostics = {
        **files[-1].diagnostics,
        "preserve_series_match": True,
    }
    db_session.add_all(files)
    await db_session.flush()

    async def never_cancel(_session: AsyncSession, _job_id: int) -> None:
        return None

    summary = await file_matching._summarize_import_series_files(
        db_session,
        imported_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        cv_match_threshold=job.cv_match_threshold,
        raise_if_cancelled=never_cancel,
        job_id=job.id,
    )
    diagnostics = summary.invalidation_diagnostics or {}
    assert summary.series_invalidated is True
    assert diagnostics["metadata_conflict_files"] == file_count
    assert imported_series.cv_id == canonical.comicvine_id


@pytest.mark.asyncio
async def test_file_page_commit_is_cancellable_and_resume_is_exact(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, issues = await _canonical_series(
        db_session,
        title="Cancellation",
        issue_count=21,
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        cv_id=canonical.comicvine_id,
        cv_match_score=0.99,
        file_count=len(issues),
        files_total=len(issues),
    )
    db_session.add(imported_series)
    await db_session.flush()
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=imported_series.id,
            ordinal=index,
            issue_number=float(index),
            series_name=canonical.title,
        )
        for index in range(1, len(issues) + 1)
    ]
    db_session.add_all(files)
    await db_session.flush()

    monkeypatch.setattr(file_matching, "_FILE_PAGE_SIZE", 7)
    original_loader = file_matching._load_eligible_import_file_page
    observed_page_sizes: list[int] = []

    async def tracked_loader(*args: object, **kwargs: object) -> list[ImportedFile]:
        page = await original_loader(*args, **kwargs)  # type: ignore[arg-type]
        observed_page_sizes.append(len(page))
        return page

    monkeypatch.setattr(file_matching, "_load_eligible_import_file_page", tracked_loader)
    service, provider = _service_with_provider_spy()

    async def cancel_after_first_commit(session: AsyncSession, _job_id: int) -> None:
        completed = await session.scalar(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_series_id == imported_series.id,
                ImportedFile.status != ImportedFileStatus.PENDING,
            )
        )
        if int(completed or 0) >= 7:
            raise JobCancelledError("generated page checkpoint")

    monkeypatch.setattr(service, "_raise_if_job_cancelled", cancel_after_first_commit)
    with pytest.raises(JobCancelledError, match="generated page checkpoint"):
        await service._run_file_matching(db_session, job)

    committed_statuses = list(
        (
            await db_session.execute(
                select(ImportedFile.status)
                .where(ImportedFile.import_series_id == imported_series.id)
                .order_by(ImportedFile.id)
            )
        ).scalars()
    )
    assert committed_statuses[:7] == [ImportedFileStatus.MATCHED] * 7
    assert committed_statuses[7:] == [ImportedFileStatus.PENDING] * 14

    resumed_service, resumed_provider = _service_with_provider_spy()
    resumed_progress: list[int] = []

    async def capture_resumed_progress(event: object) -> None:
        progress = getattr(event, "progress", None)
        if isinstance(progress, int):
            resumed_progress.append(progress)

    await resumed_service._run_file_matching(
        db_session,
        job,
        progress_callback=capture_resumed_progress,
    )
    resumed_files = list(
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_series_id == imported_series.id)
                .order_by(ImportedFile.id)
            )
        ).scalars()
    )
    assert {item.status for item in resumed_files} == {ImportedFileStatus.MATCHED}
    assert [item.matched_issue_id for item in resumed_files] == [issue.id for issue in issues]
    assert max(observed_page_sizes) <= 7
    assert provider.metadata_calls == []
    assert resumed_provider.metadata_calls == []
    assert resumed_progress == sorted(resumed_progress)
    assert resumed_progress[-1] == 99


@pytest.mark.asyncio
async def test_trusted_mylar_identity_pages_without_provider_calls(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db_session)
    file_count = 21
    series_cv_id = 1_900_001
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Trusted Mylar",
        status=ImportSeriesStatus.MATCHED,
        cv_id=series_cv_id,
        cv_title="Trusted Mylar",
        cv_match_method="mylar3_cv_id",
        cv_match_score=1.0,
        cv_issue_count=100_000,
        file_count=file_count,
        files_total=file_count,
    )
    db_session.add(imported_series)
    await db_session.flush()
    files = [
        _imported_file(
            job_id=job.id,
            import_series_id=imported_series.id,
            ordinal=index,
            issue_number=float(index),
            series_name=imported_series.raw_series_name,
        )
        for index in range(1, file_count + 1)
    ]
    for index, imp_file in enumerate(files, start=1):
        imp_file.comicvine_issue_id = 2_900_000 + index
        imp_file.diagnostics = {
            "comicvine_series_id": series_cv_id,
            "metadata_signals": {
                "comicvine_series_id": "mylar3",
                "comicvine_issue_id": "mylar3",
            },
            "source_metadata": {
                "mylar3_issue": {"title": f"Trusted Issue {index}"},
            },
        }
    db_session.add_all(files)
    await db_session.flush()
    monkeypatch.setattr(file_matching, "_FILE_PAGE_SIZE", 7)

    service, provider = _service_with_provider_spy()
    await service._run_file_matching(db_session, job)

    matched_files = list(
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_series_id == imported_series.id)
                .order_by(ImportedFile.id)
            )
        ).scalars()
    )
    assert [item.status for item in matched_files] == [ImportedFileStatus.MATCHED] * file_count
    assert [item.matched_issue_cv_id for item in matched_files] == [
        2_900_000 + index for index in range(1, file_count + 1)
    ]
    assert provider.metadata_calls == []


@pytest.mark.asyncio
async def test_same_issue_conflict_precedence_survives_file_pages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, issues = await _canonical_series(
        db_session,
        title="Paged Variants",
        issue_count=1,
    )
    job = await _job(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=canonical.title,
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=61,
        files_total=61,
    )
    db_session.add(imported_series)
    await db_session.flush()
    db_session.add_all(
        [
            _imported_file(
                job_id=job.id,
                import_series_id=imported_series.id,
                ordinal=index,
                issue_number=1.0,
                series_name=canonical.title,
                file_size=10_000 + index,
            )
            for index in range(1, 62)
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(file_matching, "_FILE_PAGE_SIZE", 7)
    monkeypatch.setattr(file_matching, "_TARGET_COHORT_PAGE_SIZE", 3)

    service, provider = _service_with_provider_spy()
    await service._run_file_matching(db_session, job)
    matched_files = list(
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_series_id == imported_series.id)
                .order_by(ImportedFile.id)
            )
        ).scalars()
    )
    preferred = [item for item in matched_files if item.is_preferred]
    assert {item.status for item in matched_files} == {ImportedFileStatus.CONFLICT}
    assert len({item.conflict_group_id for item in matched_files}) == 1
    assert preferred == [matched_files[-1]]
    assert preferred[0].matched_issue_id == issues[0].id
    assert (preferred[0].diagnostics or {}).get("group_size") == 61
    assert provider.metadata_calls == []


@pytest.mark.asyncio
async def test_cross_series_conflict_rebuild_pages_without_changing_precedence(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_count = 9
    canonical, issues = await _canonical_series(
        db_session,
        title="Cross Buckets",
        issue_count=cohort_count,
    )
    job = await _job(db_session)
    imported_series = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Cross Buckets {issue_index}-{copy_index}",
            status=ImportSeriesStatus.MATCHED,
            series_id=canonical.id,
            file_count=1,
            files_total=1,
        )
        for issue_index in range(1, cohort_count + 1)
        for copy_index in range(2)
    ]
    db_session.add_all(imported_series)
    await db_session.flush()
    db_session.add_all(
        [
            _imported_file(
                job_id=job.id,
                import_series_id=series.id,
                ordinal=index,
                issue_number=float((index - 1) // 2 + 1),
                series_name=canonical.title,
                file_size=20_000 + index,
            )
            for index, series in enumerate(imported_series, start=1)
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(file_matching, "_SERIES_PAGE_SIZE", 3)
    monkeypatch.setattr(file_matching, "_TARGET_COHORT_PAGE_SIZE", 2)

    service, provider = _service_with_provider_spy()
    cross_scan_count = 0

    def count_cross_scans(
        _conn: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        context: object,
        _executemany: bool,
    ) -> None:
        nonlocal cross_scan_count
        execution_options = getattr(context, "execution_options", {})
        if execution_options.get("pullbox_cohort_scan") == "cross_conflict":
            cross_scan_count += 1

    assert db_session.bind is not None
    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", count_cross_scans)
    try:
        await service._run_file_matching(db_session, job)
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_cross_scans)
    matched_files = list(
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_job_id == job.id)
                .order_by(ImportedFile.id)
            )
        ).scalars()
    )
    preferred = [item for item in matched_files if item.is_preferred]
    assert {item.status for item in matched_files} == {ImportedFileStatus.CONFLICT}
    assert {item.conflict_group_id for item in matched_files} == set(range(1, cohort_count + 1))
    assert preferred == matched_files[1::2]
    assert [item.matched_issue_id for item in preferred] == [issue.id for issue in issues]
    assert {(item.diagnostics or {}).get("scope") for item in preferred} == {"cross_series"}
    assert cross_scan_count == (
        len(file_matching._CROSS_TARGET_COHORT_KINDS) * len(file_matching._FILE_TARGET_COHORT_KINDS)
    )
    assert provider.metadata_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inactive_status",
    [ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.SKIPPED],
)
async def test_cross_series_rebuild_does_not_promote_ineligible_series_file(
    db_session: AsyncSession,
    inactive_status: ImportSeriesStatus,
) -> None:
    canonical, issues = await _canonical_series(
        db_session,
        title="Stale Cross Conflict",
        issue_count=1,
    )
    job = await _job(db_session)
    inactive_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Stale Cross Conflict Inactive",
        status=inactive_status,
        series_id=canonical.id,
        file_count=1,
        files_total=1,
        files_matched=0,
        files_conflict=1,
    )
    active_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Stale Cross Conflict Active",
        status=ImportSeriesStatus.MATCHED,
        series_id=canonical.id,
        file_count=1,
        files_total=1,
        files_matched=0,
        files_conflict=1,
    )
    db_session.add_all([inactive_series, active_series])
    await db_session.flush()
    inactive_file = _imported_file(
        job_id=job.id,
        import_series_id=inactive_series.id,
        ordinal=1,
        issue_number=1.0,
        series_name=inactive_series.raw_series_name,
    )
    active_file = _imported_file(
        job_id=job.id,
        import_series_id=active_series.id,
        ordinal=2,
        issue_number=1.0,
        series_name=active_series.raw_series_name,
    )
    for imp_file in (inactive_file, active_file):
        imp_file.status = ImportedFileStatus.CONFLICT
        imp_file.matched_issue_id = issues[0].id
        imp_file.conflict_group_id = 1
        imp_file.diagnostics = {
            "kind": "file_conflict",
            "scope": "cross_series",
            "previous_diagnostics": {"kind": "file_match"},
        }
    db_session.add_all([inactive_file, active_file])
    await db_session.flush()

    async def never_cancel(_session: AsyncSession, _job_id: int) -> None:
        return None

    counter = await file_matching._rebuild_cross_series_conflicts(
        db_session,
        job,
        conflict_group_counter=1,
        is_duplicate_series=lambda _series: False,
        log_event=AsyncMock(),
        raise_if_cancelled=never_cancel,
    )

    assert counter == 1
    assert inactive_file.status == ImportedFileStatus.CONFLICT
    assert inactive_file.conflict_group_id == 1
    assert inactive_series.files_matched == 0
    assert inactive_series.files_conflict == 1
    assert active_file.status == ImportedFileStatus.MATCHED
    assert active_file.conflict_group_id is None
    assert active_series.files_matched == 1
    assert active_series.files_conflict == 0
